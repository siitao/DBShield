import datetime
import logging
import traceback

from django.contrib.auth import authenticate
from django.contrib.auth.models import Group

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


class DBShieldAuth(object):
    def __init__(self, request):
        self.request = request
        self.sys_config = SysConfig()

    @staticmethod
    def challenge(username=None, password=None):
        # 仅验证密码, 验证成功返回 user 对象, 清空计数器
        user = authenticate(username=username, password=password)
        # 登录成功
        if user:
            # 如果登录成功, 登录失败次数重置为0
            user.failed_login_count = 0
            user.save()
            return user

    def authenticate(self):
        username = self.request.POST.get("username")
        password = self.request.POST.get("password")
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
        except:
            logger.error("验证用户密码时报错")
            logger.error(traceback.format_exc())
            return {"status": 1, "msg": f"服务异常，请联系管理员处理", "data": ""}
        # 已存在用户, 验证是否在锁期间
        # 读取配置文件
        lock_count = int(self.sys_config.get("lock_cnt_threshold", 5))
        lock_time = int(self.sys_config.get("lock_time_threshold", 60 * 5))
        # 验证是否在锁, 分了几个if 防止代码太长
        if user.failed_login_count and user.last_login_failed_at:
            if user.failed_login_count >= lock_count:
                now = datetime.datetime.now()
                if (
                    user.last_login_failed_at + datetime.timedelta(seconds=lock_time)
                    > now
                ):
                    return {
                        "status": 3,
                        "msg": f"登录失败超过限制，该账号已被锁定！请等候大约{lock_time}秒再试",
                        "data": "",
                    }
                else:
                    # 如果锁已超时, 重置失败次数
                    user.failed_login_count = 0
                    user.save()
        authenticated_user = self.challenge(username=username, password=password)
        if authenticated_user:
            if not authenticated_user.last_login:
                init_user(authenticated_user)
            return {"status": 0, "msg": "ok", "data": authenticated_user}
        user.failed_login_count += 1
        user.last_login_failed_at = datetime.datetime.now()
        user.save()
        return {"status": 1, "msg": "用户名或密码错误，请重新输入！", "data": ""}
