# -*- coding: UTF-8 -*-
"""JWT 认证的安全收口（认证类部分）。

背景：simplejwt 自带的 ``TokenObtainPairView`` 只走 Django ``authenticate()``，
不会经过 ``common.auth.DBShieldAuth`` 的失败锁定和两步验证逻辑。结果是任何
知道密码的人都可以直接 ``POST /api/auth/token/`` 拿到令牌，绕过 2FA 与登录
锁定。

本模块只放 DRF 启动期就会被导入的认证类，避免在这里 import simplejwt 的
``views`` 造成 ``rest_framework.views`` 循环导入；签发令牌的序列化器/视图在
``sql_api.token_view``。

``TwoFactorAwareJWTAuthentication`` 保证：即使旧版本已经签发了令牌，持有
2FA 账号的令牌也无法通过 DRF 认证（防止历史令牌继续绕过）。
"""

from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication

from common.config import SysConfig
from sql.models import TwoFactorAuthConfig


def requires_two_factor(user) -> bool:
    """账号是否必须完成两步验证。

    用户已绑定任一 2FA 方式，或系统开启 ``enforce_2fa``，均视为必须验证。
    """
    if user is None:
        return False
    if TwoFactorAuthConfig.objects.filter(user=user).exists():
        return True
    return bool(SysConfig().get("enforce_2fa"))


class TwoFactorAwareJWTAuthentication(JWTAuthentication):
    """拒绝 2FA 账号使用 JWT（含修复前已经签发的历史令牌）。"""

    def get_user(self, validated_token):
        user = super().get_user(validated_token)
        if requires_two_factor(user):
            raise AuthenticationFailed(
                "该账号已启用两步验证，请通过 Web 登录完成验证",
                code="2fa_required",
            )
        return user
