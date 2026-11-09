# -*- coding: UTF-8 -*-
"""P0 安全回归：JWT/2FA/归档审批状态。

这些用例不依赖真实外部服务；TOTP 用 pyotp 本地生成，归档任务用 mock 拦截。
"""

import pyotp
import pytest
from django.contrib.auth.models import Permission
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from common.config import SysConfig
from common.utils.const import WorkflowStatus
from sql.archiver import archive
from sql.models import TwoFactorAuthConfig


def _set_password(user, password="Passw0rd!123"):
    user.set_password(password)
    user.save(update_fields=["password"])
    return password


def _grant(user, codename):
    user.user_permissions.add(Permission.objects.get(codename=codename))


def _start_2fa_session(client, user, password):
    """密码校验后拿到 2FA 临时 session，并写入测试客户端 cookie。"""
    resp = client.post(
        "/api/v1/authenticate/",
        {"username": user.username, "password": password},
        format="json",
    )
    assert resp.status_code == 200, resp.content
    session_key = resp.json().get("data")
    assert session_key, resp.json()
    client.cookies["sessionid"] = session_key
    return session_key


# ---------- P0-1: JWT ----------


@pytest.mark.django_db
def test_token_endpoint_rejects_user_with_2fa(normal_user):
    password = _set_password(normal_user)
    TwoFactorAuthConfig.objects.create(
        user=normal_user,
        username=normal_user.username,
        auth_type="totp",
        secret_key=pyotp.random_base32(),
    )

    client = APIClient()
    resp = client.post(
        "/api/auth/token/",
        {"username": normal_user.username, "password": password},
        format="json",
    )

    assert resp.status_code == 401
    assert "两步验证" in str(resp.json())


@pytest.mark.django_db
def test_token_endpoint_still_works_for_non_2fa_superuser(super_user):
    password = _set_password(super_user)
    client = APIClient()

    resp = client.post(
        "/api/auth/token/",
        {"username": super_user.username, "password": password},
        format="json",
    )
    assert resp.status_code == 200, resp.content
    access = resp.json()["access"]

    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    me = client.get("/api/v1/user/")
    assert me.status_code == 200


@pytest.mark.django_db
def test_legacy_jwt_rejected_for_2fa_user(normal_user):
    """修复前签发的令牌也不能继续绕过 2FA。"""
    TwoFactorAuthConfig.objects.create(
        user=normal_user,
        username=normal_user.username,
        auth_type="totp",
        secret_key=pyotp.random_base32(),
    )
    access = RefreshToken.for_user(normal_user).access_token

    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    resp = client.get("/api/v1/user/me/")

    assert resp.status_code == 401


@pytest.mark.django_db
def test_token_endpoint_enforces_lockout(normal_user):
    password = _set_password(normal_user)
    sys_config = SysConfig()
    sys_config.set("lock_cnt_threshold", 2)
    sys_config.set("lock_time_threshold", 300)

    client = APIClient()
    for _ in range(2):
        resp = client.post(
            "/api/auth/token/",
            {"username": normal_user.username, "password": "wrong-password"},
            format="json",
        )
        assert resp.status_code == 401

    resp = client.post(
        "/api/auth/token/",
        {"username": normal_user.username, "password": password},
        format="json",
    )
    assert resp.status_code == 401
    assert "锁定" in str(resp.json())


@pytest.mark.django_db
def test_token_endpoint_respects_enforce_2fa(normal_user):
    password = _set_password(normal_user)
    SysConfig().set("enforce_2fa", True)

    client = APIClient()
    resp = client.post(
        "/api/auth/token/",
        {"username": normal_user.username, "password": password},
        format="json",
    )

    assert resp.status_code == 401
    assert "两步验证" in str(resp.json())


# ---------- P0-2: 2FA 校验只认服务端绑定材料 ----------


@pytest.mark.django_db
def test_2fa_verify_ignores_client_supplied_key(normal_user):
    password = _set_password(normal_user)
    victim_secret = pyotp.random_base32()
    TwoFactorAuthConfig.objects.create(
        user=normal_user,
        username=normal_user.username,
        auth_type="totp",
        secret_key=victim_secret,
    )

    client = APIClient()
    _start_2fa_session(client, normal_user, password)

    # 攻击者用自己的 secret 计算验证码，并把自己的 key 一起提交：必须失败
    attacker_secret = pyotp.random_base32()
    attacker_otp = pyotp.TOTP(attacker_secret).now()
    resp = client.post(
        "/api/v1/user/2fa/verify/",
        {
            "engineer": normal_user.username,
            "otp": attacker_otp,
            "key": attacker_secret,
            "auth_type": "totp",
        },
        format="json",
    )
    assert resp.json()["status"] == 1

    # 正确验证码 + 攻击者 key：key 必须被忽略，验证码按服务端 secret 校验通过
    victim_otp = pyotp.TOTP(victim_secret).now()
    resp = client.post(
        "/api/v1/user/2fa/verify/",
        {
            "engineer": normal_user.username,
            "otp": victim_otp,
            "key": attacker_secret,
            "auth_type": "totp",
        },
        format="json",
    )
    assert resp.json()["status"] == 0, resp.json()
    assert client.get("/api/v1/user/me/").status_code == 200


@pytest.mark.django_db
def test_2fa_cannot_be_disabled_with_password_only(normal_user):
    password = _set_password(normal_user)
    TwoFactorAuthConfig.objects.create(
        user=normal_user,
        username=normal_user.username,
        auth_type="totp",
        secret_key=pyotp.random_base32(),
    )

    client = APIClient()
    _start_2fa_session(client, normal_user, password)

    resp = client.post(
        "/api/v1/user/2fa/",
        {
            "engineer": normal_user.username,
            "enable": "false",
            "auth_type": "totp",
        },
        format="json",
    )

    assert resp.json()["status"] == 1
    assert TwoFactorAuthConfig.objects.filter(
        user=normal_user, auth_type="totp"
    ).exists()


# ---------- P0-3: 归档必须已审核通过 ----------


@pytest.mark.django_db
def test_archive_once_rejects_unapproved(
    archive_apply, resource_group, normal_user, mocker
):
    resource_group.users_set.add(normal_user)
    _grant(normal_user, "archive_mgt")
    async_task = mocker.patch("sql_api.api_archiver.async_task")

    client = APIClient()
    client.force_authenticate(user=normal_user)
    resp = client.get(f"/api/v1/archive/once/?archive_id={archive_apply.id}")

    assert resp.json()["status"] == 1
    assert "未审核通过" in resp.json()["msg"]
    async_task.assert_not_called()


@pytest.mark.django_db
def test_archive_once_allows_approved(
    archive_apply, resource_group, normal_user, mocker
):
    resource_group.users_set.add(normal_user)
    _grant(normal_user, "archive_mgt")
    archive_apply.state = True
    archive_apply.status = WorkflowStatus.PASSED
    archive_apply.save(update_fields=["state", "status"])
    async_task = mocker.patch("sql_api.api_archiver.async_task")

    client = APIClient()
    client.force_authenticate(user=normal_user)
    resp = client.get(f"/api/v1/archive/once/?archive_id={archive_apply.id}")

    assert resp.json()["status"] == 0
    async_task.assert_called_once()


@pytest.mark.django_db
def test_archive_task_refuses_unapproved(archive_apply):
    with pytest.raises(Exception, match="未启用或未审核通过"):
        archive(archive_apply.id)


@pytest.mark.django_db
def test_archive_switch_rejects_enabling_unapproved(
    archive_apply, resource_group, normal_user
):
    resource_group.users_set.add(normal_user)
    _grant(normal_user, "archive_mgt")

    client = APIClient()
    client.force_authenticate(user=normal_user)
    resp = client.post(
        "/api/v1/archive/switch/",
        {"archive_id": archive_apply.id, "state": "true"},
        format="json",
    )

    assert resp.json()["status"] == 1
    archive_apply.refresh_from_db()
    assert archive_apply.state is False


@pytest.mark.django_db
def test_archive_apply_rejects_unknown_mode(db_instance, resource_group, normal_user):
    resource_group.users_set.add(normal_user)
    resource_group.instance_set.add(db_instance)
    _grant(normal_user, "archive_apply")

    client = APIClient()
    client.force_authenticate(user=normal_user)
    resp = client.post(
        "/api/v1/archive/apply/",
        {
            "title": "bogus mode",
            "group_name": resource_group.group_name,
            "src_instance_name": db_instance.instance_name,
            "src_db_name": "db1",
            "src_table_name": "t1",
            "mode": "bogus",
            "condition": "1=1",
            "no_delete": "true",
            "sleep": 1,
        },
        format="json",
    )

    assert resp.json()["status"] == 1
    assert "归档模式不合法" in resp.json()["msg"]
