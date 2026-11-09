import datetime
import logging
import traceback

from django.contrib.auth import authenticate
from django.contrib.auth.models import Group
from django.db.models import F

from common.config import SysConfig
from sql.models import Users, ResourceGroup

logger = logging.getLogger("default")


def init_user(user):
    """
    给用户关联默认资源组和权限组
    :param user:
    :return:
    """
    # 添加到默认权限组
    default_auth_group = SysConfig().get("default_auth_group", "")
    if default_auth_group:
        default_auth_group = default_auth_group.split(",")
        [
            user.groups.add(group)
            for group in Group.objects.filter(name__in=default_auth_group)
        ]

    # 添加到默认资源组
    default_resource_group = SysConfig().get("default_resource_group", "")
    if default_resource_group:
        default_resource_group = default_resource_group.split(",")
        [
            user.resource_group.add(group)
            for group in ResourceGroup.objects.filter(
                group_name__in=default_resource_group
            )
        ]


def check_login_locked(user, sys_config=None):
    """检查账号是否处于登录锁定期。

    返回 (locked, message)。锁定超时后会自动清零失败计数。
    计数与阈值语义与旧 DBShieldAuth 保持一致，供 Web 登录和 API Token 共用。
    """
    sys_config = sys_config or SysConfig()
    try:
        lock_count = int(sys_config.get("lock_cnt_threshold", 5) or 5)
        lock_time = int(sys_config.get("lock_time_threshold", 60 * 5) or 60 * 5)
    except (TypeError, ValueError):
        lock_count, lock_time = 5, 60 * 5

    if user.failed_login_count and user.last_login_failed_at:
        if user.failed_login_count >= lock_count:
            now = datetime.datetime.now()
            if user.last_login_failed_at + datetime.timedelta(seconds=lock_time) > now:
                return True, (
                    f"登录失败超过限制，该账号已被锁定！" f"请等候大约{lock_time}秒再试"
                )
            # 锁定已超时，重置失败次数
            reset_login_failure(user)
    return False, ""


def record_login_failure(user):
    """原子累加登录失败次数，避免并发爆破少计。"""
    Users.objects.filter(pk=user.pk).update(
        failed_login_count=F("failed_login_count") + 1,
        last_login_failed_at=datetime.datetime.now(),
    )
    user.refresh_from_db(fields=["failed_login_count", "last_login_failed_at"])


def reset_login_failure(user):
    """登录成功后清零失败计数。"""
    if user.failed_login_count:
        Users.objects.filter(pk=user.pk).update(failed_login_count=0)
        user.failed_login_count = 0


class DBShieldAuth(object):
    def __init__(self, request, post_data=None):
        self.request = request
        self.sys_config = SysConfig()
        # form-encoded 请求由 request.POST 提供；JSON 请求由调用方传 post_data
        self.post_data = post_data if post_data is not None else request.POST

    @staticmethod
    def challenge(username=None, password=None):
        # 仅验证密码, 验证成功返回 user 对象, 清空计数器
        user = authenticate(username=username, password=password)
        # 登录成功
        if user:
            # 如果登录成功, 登录失败次数重置为0
            reset_login_failure(user)
            return user

    def authenticate(self):
        username = self.post_data.get("username")
        password = self.post_data.get("password")
        # 确认用户是否已经存在
        try:
            user = Users.objects.get(username=username)
        except Users.DoesNotExist:
            authenticated_user = self.challenge(username=username, password=password)
            if authenticated_user:
                # ldap 首次登录逻辑
                init_user(authenticated_user)
                return {"status": 0, "msg": "ok", "data": authenticated_user}
            else:
                return {
                    "status": 1,
                    "msg": "用户名或密码错误，请重新输入！",
                    "data": "",
                }
        except Exception:
            logger.error("验证用户密码时报错")
            logger.error(traceback.format_exc())
            return {"status": 1, "msg": f"服务异常，请联系管理员处理", "data": ""}
        # 已存在用户, 验证是否在锁期间
        locked, lock_message = check_login_locked(user, self.sys_config)
        if locked:
            return {"status": 3, "msg": lock_message, "data": ""}
        authenticated_user = self.challenge(username=username, password=password)
        if authenticated_user:
            if not authenticated_user.last_login:
                init_user(authenticated_user)
            return {"status": 0, "msg": "ok", "data": authenticated_user}
        record_login_failure(user)
        return {"status": 1, "msg": "用户名或密码错误，请重新输入！", "data": ""}
