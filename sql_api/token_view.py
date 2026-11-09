# -*- coding: UTF-8 -*-
"""JWT 签发视图：在签发令牌前执行与 Web 登录一致的锁定/2FA 策略。

拆到独立模块的原因：``token_auth`` 会被 DRF 在启动期通过
``DEFAULT_AUTHENTICATION_CLASSES`` 导入，若那里 import simplejwt 的
``views`` 会在 ``rest_framework.views`` 尚未完成初始化时形成循环导入。
"""

from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.views import TokenObtainPairView

from common.auth import (
    check_login_locked,
    record_login_failure,
    reset_login_failure,
)
from sql.models import Users

from .token_auth import requires_two_factor


class TwoFactorAwareTokenObtainPairSerializer(TokenObtainPairSerializer):
    """签发 JWT 前执行锁定检查，并拒绝 2FA 账号走 API 令牌。"""

    def validate(self, attrs):
        username = attrs.get(self.username_field)
        user = Users.objects.filter(username=username).first() if username else None

        if user:
            locked, message = check_login_locked(user)
            if locked:
                raise AuthenticationFailed(message, code="account_locked")

        try:
            data = super().validate(attrs)
        except AuthenticationFailed:
            # 密码错误/账号不可用：与 Web 登录共用失败计数
            if user:
                record_login_failure(user)
            raise

        if requires_two_factor(self.user):
            # 密码已验证但仍需第二因素。不签发令牌，也不累加失败计数。
            reset_login_failure(self.user)
            raise AuthenticationFailed(
                "该账号已启用两步验证，请通过 Web 登录完成验证后再使用 API 令牌",
                code="2fa_required",
            )

        reset_login_failure(self.user)
        return data


class TwoFactorAwareTokenObtainPairView(TokenObtainPairView):
    serializer_class = TwoFactorAwareTokenObtainPairSerializer
