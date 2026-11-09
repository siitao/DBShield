# -*- coding: UTF-8 -*-
"""认证方式配置 API（superuser 专用）。

提供：
- GET  /api/v1/auth_config/        读取当前 auth_provider + 各方式参数
- POST /api/v1/auth_config/        保存到 DB（可选同时触发重载，由 body 的 reload 决定）
- POST /api/v1/auth_config/reload/ 单独触发重载（写 .env + reload settings + 清 URL 缓存）
- POST /api/v1/auth_config/test/   测试连通性（LDAP bind / OIDC well-known / CAS 可达）

本地账号密码登录始终启用，不受此配置影响。
"""

import logging

import requests
from django.http import JsonResponse
from drf_spectacular.utils import extend_schema
from rest_framework import views
from rest_framework import permissions
from rest_framework.response import Response

from common.auth_settings_reload import reload_auth_config
from common.config import SysConfig
from sql_api.api_config import IsSuperuser

logger = logging.getLogger(__name__)

# 合法的 auth_provider 取值
PROVIDERS = ("local", "ldap", "oidc", "dingding", "cas")

# 多 worker 部署的生效说明：reload 只影响处理本次请求的 worker，
# 其他 worker（以及 CAS 的 middleware 链）需要重启进程才能全量生效。
MULTI_WORKER_RELOAD_HINT = (
    "若使用 Docker/gunicorn 多 worker 部署，请重启 dbshield 服务，"
    "确保所有 worker 生效（CAS 认证方式必须重启）。"
)


class LoginOptionsView(views.APIView):
    """登录页可选项（AllowAny，未登录态可访问）。

    返回当前启用的 SSO 方式、按钮文案、登录跳转 URL，供 SPA 登录页渲染 SSO 按钮。
    优先取数据库 auth_provider；未配置时回退到 settings.ENABLE_*（兼容旧 .env 部署）。
    """

    permission_classes = [permissions.AllowAny]

    @extend_schema(
        summary="登录页选项",
        description="返回当前启用的 SSO 登录方式（oidc/dingding/cas）、按钮名称、登录跳转 URL。AllowAny。",
    )
    def get(self, request):
        auth_provider = SysConfig().get("auth_provider")
        # 未配置数据库 auth_provider 时，回退到启动时的 settings.ENABLE_*
        if auth_provider not in ("ldap", "oidc", "dingding", "cas"):
            from django.conf import settings

            if getattr(settings, "ENABLE_OIDC", False):
                auth_provider = "oidc"
            elif getattr(settings, "ENABLE_DINGDING", False):
                auth_provider = "dingding"
            elif getattr(settings, "ENABLE_CAS", False):
                auth_provider = "cas"
            else:
                auth_provider = "local"

        oidc_btn_name = SysConfig().get("oidc_btn_name", "以OIDC登录")
        return Response(
            {
                "auth_provider": auth_provider,
                "oidc_enabled": auth_provider == "oidc",
                "dingding_enabled": auth_provider == "dingding",
                "cas_enabled": auth_provider == "cas",
                "oidc_btn_name": oidc_btn_name,
                # SSO 登录入口（整页跳转，非 axios）
                "oidc_login_url": "/oidc/authenticate/",
                "dingding_login_url": "/dingding/authenticate/",
                "cas_login_url": "/cas/authenticate/",
            }
        )


class AuthConfigView(views.APIView):
    """读取 / 保存认证方式配置。

    GET  返回 {auth_provider, ...各方式参数...}。
    POST body: {auth_provider: "local|ldap|oidc|dingding|cas", params: {...},
                reload: bool} —— 保存到 DB；若 reload=true 则同时触发重载。
    """

    permission_classes = [IsSuperuser]

    @extend_schema(
        summary="认证方式配置（读取）",
        description="返回当前外部认证方式（auth_provider）及各方式参数（superuser 专用）。",
    )
    def get(self, request):
        return Response(SysConfig().get_auth_config())

    @extend_schema(
        summary="认证方式配置（保存）",
        description="保存认证方式到数据库。body: {auth_provider, params, reload?}。"
        "本地账号密码登录始终启用。",
    )
    def post(self, request):
        data = request.data or {}
        provider = (data.get("auth_provider") or "local").strip()
        if provider not in PROVIDERS:
            return JsonResponse(
                {"status": 1, "msg": f"不支持的认证方式: {provider}"}, status=400
            )

        params = data.get("params") or {}
        if not isinstance(params, dict):
            return JsonResponse({"status": 1, "msg": "params 必须是对象"}, status=400)

        try:
            sys_config = SysConfig()
            sys_config.set_auth_config(provider, params)
        except Exception as e:
            logger.exception("保存认证配置失败")
            return JsonResponse({"status": 1, "msg": f"保存失败: {e}"}, status=500)

        msg = "认证配置已保存"
        if data.get("reload"):
            ok, reload_msg = reload_auth_config()
            status = 0 if ok else 1
            suffix = f"；{MULTI_WORKER_RELOAD_HINT}" if ok else ""
            return JsonResponse(
                {"status": status, "msg": f"{msg}；{reload_msg}{suffix}"}
            )
        msg += "。点击「重载认证配置」后生效。"
        return JsonResponse({"status": 0, "msg": msg})


class ReloadAuthConfigView(views.APIView):
    """单独触发认证配置重载（写 .env + reload settings + 清 URL 缓存）。"""

    permission_classes = [IsSuperuser]

    @extend_schema(
        summary="重载认证配置",
        description="把数据库中的认证配置写回 .env 并重新加载 settings；"
        "多 worker 部署需重启服务让全部 worker 生效。",
    )
    def post(self, request):
        ok, msg = reload_auth_config()
        if ok:
            msg = f"{msg}；{MULTI_WORKER_RELOAD_HINT}"
        return JsonResponse({"status": 0 if ok else 1, "msg": msg})


class TestAuthConnectionView(views.APIView):
    """测试外部认证连通性（不落库，直接用 body 提交的临时值）。

    body: {provider: "ldap|oidc|cas", ...params}
    钉钉无独立测试端点，前端会提示「保存后用扫码验证」。
    """

    permission_classes = [IsSuperuser]

    @extend_schema(
        summary="认证连接测试",
        description="测试 LDAP bind / OIDC well-known / CAS 服务端可达性。",
    )
    def post(self, request):
        data = request.data or {}
        provider = (data.get("provider") or "").strip()
        try:
            if provider == "ldap":
                return self._test_ldap(data)
            if provider == "oidc":
                return self._test_oidc(data)
            if provider == "cas":
                return self._test_cas(data)
            if provider == "dingding":
                return JsonResponse(
                    {
                        "status": 0,
                        "msg": "钉钉无独立测试端点，请保存配置后用扫码登录验证。",
                    }
                )
            return JsonResponse(
                {"status": 1, "msg": f"不支持的认证方式: {provider}"}, status=400
            )
        except Exception as e:
            logger.exception("认证连接测试异常")
            return JsonResponse({"status": 1, "msg": f"测试失败: {e}"}, status=500)

    def _test_ldap(self, data):
        """测试 LDAP 连通性。

        优先用 ldap3（纯 Python，Windows 无需编译）；若已装 python-ldap 则用它
        （与生产认证同库）；都没有则退化为 socket 端口探测，至少能判断服务可达。
        生产环境的实际登录认证仍走 python-ldap（django-auth-ldap 依赖），此处仅做连通性测试。
        """
        server_uri = (data.get("auth_ldap_server_uri") or "").strip()
        if not server_uri:
            return JsonResponse({"status": 1, "msg": "LDAP 服务器地址不能为空"})

        bind_dn = (data.get("auth_ldap_bind_dn") or "").strip()
        bind_pw = data.get("auth_ldap_bind_password") or ""

        # 方案 1：ldap3（纯 Python，首选）
        try:
            import ldap3

            server = ldap3.Server(server_uri, connect_timeout=5)
            conn = ldap3.Connection(
                server, user=bind_dn or None, password=bind_pw or None, auto_bind=True
            )
            try:
                bound = conn.bind()
            finally:
                conn.unbind()
            if bound:
                return JsonResponse({"status": 0, "msg": "LDAP 连接成功（ldap3）"})
            return JsonResponse(
                {"status": 1, "msg": "LDAP bind 失败：凭据无效或服务拒绝"}
            )
        except ImportError:
            pass  # 降级到方案 2
        except Exception as e:
            return JsonResponse({"status": 1, "msg": f"LDAP 连接失败: {e}"})

        # 方案 2：python-ldap（生产认证库，已装时用）
        try:
            import ldap

            conn = ldap.initialize(server_uri)
            conn.set_option(ldap.OPT_NETWORK_TIMEOUT, 5)
            conn.simple_bind_s(bind_dn, bind_pw)
            conn.unbind_s()
            return JsonResponse({"status": 0, "msg": "LDAP 连接成功（python-ldap）"})
        except ImportError:
            pass  # 降级到方案 3
        except Exception as e:
            return JsonResponse({"status": 1, "msg": f"LDAP 连接失败: {e}"})

        # 方案 3：socket 端口探测（两个库都没装时的兜底）
        try:
            from urllib.parse import urlparse

            parsed = urlparse(server_uri)
            host = parsed.hostname
            port = parsed.port or (636 if parsed.scheme == "ldaps" else 389)
            if not host:
                return JsonResponse(
                    {"status": 1, "msg": f"无法解析 LDAP 地址: {server_uri}"}
                )
            import socket

            with socket.create_connection((host, port), timeout=5):
                return JsonResponse(
                    {
                        "status": 0,
                        "msg": (
                            f"LDAP 服务端口可达（{host}:{port}）。"
                            "未安装 ldap3/python-ldap，仅做了端口探测；"
                            "如需验证 bind，请 pip install ldap3（纯 Python，无需编译）。"
                        ),
                    }
                )
        except Exception as e:
            return JsonResponse({"status": 1, "msg": f"LDAP 连接失败: {e}"})

    def _test_oidc(self, data):
        wellknown = (data.get("oidc_rp_wellknown_url") or "").strip()
        if not wellknown:
            return JsonResponse({"status": 1, "msg": "OIDC well-known URL 不能为空"})
        try:
            resp = requests.get(wellknown, timeout=5)
            resp.raise_for_status()
            cfg = resp.json()
            endpoints = [
                "authorization_endpoint",
                "token_endpoint",
                "userinfo_endpoint",
                "jwks_uri",
            ]
            missing = [k for k in endpoints if not cfg.get(k)]
            if missing:
                return JsonResponse(
                    {
                        "status": 1,
                        "msg": f"well-known 响应缺少端点: {', '.join(missing)}",
                    }
                )
            return JsonResponse(
                {"status": 0, "msg": "OIDC well-known 拉取成功，端点齐全"}
            )
        except Exception as e:
            return JsonResponse({"status": 1, "msg": f"OIDC 连接失败: {e}"})

    def _test_cas(self, data):
        server_url = (data.get("cas_server_url") or "").strip()
        if not server_url:
            return JsonResponse({"status": 1, "msg": "CAS 服务端地址不能为空"})
        try:
            resp = requests.get(server_url, timeout=5, verify=False)
            if resp.status_code < 500:
                return JsonResponse(
                    {"status": 0, "msg": f"CAS 服务端可达（HTTP {resp.status_code}）"}
                )
            return JsonResponse(
                {"status": 1, "msg": f"CAS 服务端返回 HTTP {resp.status_code}"}
            )
        except Exception as e:
            return JsonResponse({"status": 1, "msg": f"CAS 连接失败: {e}"})
