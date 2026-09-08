from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

from django.test import TestCase
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from rest_framework.test import APITestCase
from rest_framework import status
from common.config import SysConfig
from sql.utils.workflow_audit import AuditSetting
from sql.engines import ReviewSet
from sql.engines.models import ReviewResult
from sql.models import (
    ResourceGroup,
    Instance,
    AliyunRdsConfig,
    CloudAccessKey,
    Tunnel,
    SqlWorkflow,
    SqlWorkflowContent,
    WorkflowAudit,
    WorkflowLog,
    InstanceTag,
    WorkflowAuditSetting,
    TwoFactorAuthConfig,
    AIUsageLog,
)
from django.test import override_settings
import json

User = get_user_model()


class InfoTest(TestCase):
    def setUp(self) -> None:
        self.superuser = User.objects.create(username="super", is_superuser=True)
        self.client.force_login(self.superuser)

    def tearDown(self) -> None:
        self.superuser.delete()

    def test_info_api(self):
        r = self.client.get("/api/info")
        r_json = r.json()
        self.assertIsInstance(r_json["dbshield"]["version"], str)

    def test_debug_api(self):
        r = self.client.get("/api/debug")
        r_json = r.json()
        self.assertIsInstance(r_json["dbshield"]["version"], str)


class TestUser(APITestCase):
    """测试用户相关接口"""

    def setUp(self):
        self.user = User(
            username="test_user", display="测试用户", is_active=True, is_superuser=True
        )
        self.user.set_password("test_password")
        self.user.save()
        self.group = Group.objects.create(id=1, name="DBA")
        self.res_group = ResourceGroup.objects.create(group_id=1, group_name="test")
        r = self.client.post(
            "/api/auth/token/",
            {"username": "test_user", "password": "test_password"},
            format="json",
        )
        self.token = r.data["access"]
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + self.token)
        SysConfig().set("api_user_whitelist", self.user.id)

    def tearDown(self):
        self.user.delete()
        self.group.delete()
        self.res_group.delete()
        SysConfig().purge()

    def test_user_not_in_whitelist(self):
        """测试管理员不受api用户白名单参数影响"""
        SysConfig().set("api_user_whitelist", "")
        r = self.client.get("/api/v1/user/", format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.json()["count"], 1)

    def test_get_user_list(self):
        """测试获取用户清单"""
        r = self.client.get("/api/v1/user/", format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.json()["count"], 1)

    def test_create_user(self):
        """测试创建用户"""
        json_data = {
            "username": "test_user2",
            "password": "test_password2",
            "display": "测试用户2",
        }
        r = self.client.post("/api/v1/user/", json_data, format="json")
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        self.assertEqual(r.json()["username"], "test_user2")

    def test_update_user(self):
        """测试更新用户"""
        json_data = {"display": "更新中文名"}
        r = self.client.put(f"/api/v1/user/{self.user.id}/", json_data, format="json")
        user = User.objects.get(pk=self.user.id)
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(user.display, "更新中文名")

    def test_delete_user(self):
        """测试删除用户"""
        json_data = {
            "username": "test_user2",
            "password": "test_password2",
            "display": "测试用户2",
        }
        r1 = self.client.post("/api/v1/user/", json_data, format="json")
        r2 = self.client.delete(f'/api/v1/user/{r1.json()["id"]}/', format="json")
        self.assertEqual(r2.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(User.objects.filter(username="test_user2").count(), 0)

    def test_get_user_group_list(self):
        """测试获取用户组清单"""
        r = self.client.get("/api/v1/user/group/", format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.json()["count"], 1)

    def test_create_user_group(self):
        """测试创建用户组"""
        json_data = {"name": "RD"}
        r = self.client.post("/api/v1/user/group/", json_data, format="json")
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        self.assertEqual(r.json()["name"], "RD")

    def test_update_user_group(self):
        """测试更新用户组"""
        json_data = {"name": "更新用户组名称"}
        r = self.client.put(
            f"/api/v1/user/group/{self.group.id}/", json_data, format="json"
        )
        group = Group.objects.get(pk=self.group.id)
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(group.name, "更新用户组名称")

    def test_delete_user_group(self):
        """测试删除用户组"""
        r = self.client.delete(f"/api/v1/user/group/{self.group.id}/", format="json")
        self.assertEqual(r.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(Group.objects.filter(name="DBA").count(), 0)

    def test_get_resource_group_list(self):
        """测试获取资源组清单"""
        r = self.client.get("/api/v1/user/resourcegroup/", format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.json()["count"], 1)

    def test_create_resource_group(self):
        """测试创建资源组"""
        json_data = {
            "group_name": "prod",
            "ding_webhook": "https://oapi.dingtalk.com/robot/send?access_token=123",
        }
        r = self.client.post("/api/v1/user/resourcegroup/", json_data, format="json")
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        self.assertEqual(r.json()["group_name"], "prod")

    def test_update_resource_group(self):
        """测试更新资源组"""
        json_data = {"group_name": "更新资源组名称"}
        r = self.client.put(
            f"/api/v1/user/resourcegroup/{self.res_group.group_id}/",
            json_data,
            format="json",
        )
        group = ResourceGroup.objects.get(pk=self.res_group.group_id)
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(group.group_name, "更新资源组名称")

    def test_delete_resource_group(self):
        """测试删除资源组"""
        r = self.client.delete(
            f"/api/v1/user/resourcegroup/{self.res_group.group_id}/", format="json"
        )
        self.assertEqual(r.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(Group.objects.filter(name="test").count(), 0)

    def test_user_auth(self):
        """测试用户认证校验"""
        json_data = {"engineer": "test_user", "password": "test_password"}
        r = self.client.post(f"/api/v1/user/auth/", json_data, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.json(), {"status": 0, "msg": "认证成功"})

    def test_2fa_config(self):
        """测试用户配置2fa"""
        json_data = {"engineer": "test_user", "auth_type": "totp", "enable": "false"}
        r = self.client.post(f"/api/v1/user/2fa/", json_data, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(TwoFactorAuthConfig.objects.count(), 0)

    def test_2fa_save(self):
        """测试用户保存2fa配置"""
        json_data = {
            "engineer": "test_user",
            "auth_type": "totp",
            "key": "ZUGRIJZP6H7LIOAL4LH5JA4GSXXT3WOK",
        }
        r = self.client.post(f"/api/v1/user/2fa/save/", json_data, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(TwoFactorAuthConfig.objects.count(), 1)

    def test_2fa_verify(self):
        """测试2fa验证码校验"""
        json_data = {
            "engineer": "test_user",
            "otp": 123456,
            "key": "ZUGRIJZP6H7LIOAL4LH5JA4GSXXT3WOK",
            "auth_type": "totp",
        }
        r = self.client.post(f"/api/v1/user/2fa/verify/", json_data, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.json()["status"], 1)


class TestInstance(APITestCase):
    """测试实例相关接口"""

    def setUp(self):
        self.user = User(
            username="test_user", display="测试用户", is_active=True, is_superuser=True
        )
        self.user.set_password("test_password")
        self.user.save()
        self.ins = Instance.objects.create(
            instance_name="some_ins",
            type="slave",
            db_type="mysql",
            host="some_host",
            port=3306,
            user="ins_user",
            password="some_str",
        )
        self.ak = CloudAccessKey.objects.create(
            type="aliyun", key_id="abc", key_secret="abc"
        )
        self.rds = AliyunRdsConfig.objects.create(
            rds_dbinstanceid="abc", ak_id=self.ak.id, instance=self.ins
        )
        self.tunnel = Tunnel.objects.create(
            tunnel_name="one_tunnel", host="one_host", port=22
        )
        r = self.client.post(
            "/api/auth/token/",
            {"username": "test_user", "password": "test_password"},
            format="json",
        )
        self.token = r.data["access"]
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + self.token)
        SysConfig().set("api_user_whitelist", self.user.id)

    def tearDown(self):
        self.user.delete()
        Instance.objects.all().delete()
        AliyunRdsConfig.objects.all().delete()
        CloudAccessKey.objects.all().delete()
        Tunnel.objects.all().delete()
        SysConfig().purge()

    def test_get_instance_list(self):
        """测试获取实例清单"""
        r = self.client.get("/api/v1/instance/", format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.json()["count"], 1)

    def test_create_instance(self):
        """测试创建实例"""
        json_data = {
            "instance_name": "test_ins",
            "type": "master",
            "db_type": "mysql",
            "host": "some_host",
            "port": 3306,
        }
        r = self.client.post("/api/v1/instance/", json_data, format="json")
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        self.assertEqual(r.json()["instance_name"], "test_ins")

    def test_update_instance(self):
        """测试更新实例"""
        json_data = {"instance_name": "更新实例名称"}
        r = self.client.put(
            f"/api/v1/instance/{self.ins.id}/", json_data, format="json"
        )
        ins = Instance.objects.get(pk=self.ins.id)
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(ins.instance_name, "更新实例名称")

    def test_delete_instance(self):
        """测试删除实例"""
        r = self.client.delete(f"/api/v1/instance/{self.ins.id}/", format="json")
        self.assertEqual(r.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(Instance.objects.filter(instance_name="some_ins").count(), 0)

    def test_get_aliyunrds_list(self):
        """测试获取aliyunrds清单"""
        r = self.client.get("/api/v1/instance/rds/", format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.json()["count"], 1)

    def test_create_aliyunrds(self):
        """测试创建aliyunrds"""
        ins = Instance.objects.create(
            instance_name="another_ins",
            type="slave",
            db_type="mysql",
            host="another_host",
            port=3306,
        )
        json_data = {
            "rds_dbinstanceid": "bbc",
            "is_enable": True,
            "instance": ins.id,
            "ak": {
                "type": "aliyun",
                "key_id": "bbc",
                "key_secret": "bbc",
                "remark": "bbc",
            },
        }
        r = self.client.post("/api/v1/instance/rds/", json_data, format="json")
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        self.assertEqual(r.json()["rds_dbinstanceid"], "bbc")

    def test_get_tunnel_list(self):
        """测试获取隧道清单"""
        r = self.client.get("/api/v1/instance/tunnel/", format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.json()["count"], 1)

    def test_create_tunnel(self):
        """测试创建隧道"""
        json_data = {"tunnel_name": "tunnel_test", "host": "one_host", "port": 22}
        r = self.client.post("/api/v1/instance/tunnel/", json_data, format="json")
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        self.assertEqual(r.json()["tunnel_name"], "tunnel_test")

    @patch("sql_api.api_instance.resolve_table_instances")
    def test_lookup_table_instances(self, mock_resolve):
        """测试按表名查询所属实例"""
        mock_resolve.return_value = [
            {
                "id": self.ins.id,
                "name": self.ins.instance_name,
                "db_type": self.ins.db_type,
                "db_name": "archery",
            }
        ]
        r = self.client.post(
            "/api/v1/instance/table-instances/",
            {"table_name": "orders"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.json()["status"], 0)
        self.assertEqual(r.json()["msg"], "查询成功")
        self.assertEqual(r.json()["count"], 1)
        self.assertEqual(r.json()["data"][0]["name"], "some_ins")

    def test_lookup_table_instances_bad_request(self):
        """测试按表名查询参数错误时固定返回格式"""
        r = self.client.post("/api/v1/instance/table-instances/", {}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.json()["status"], 1)
        self.assertIn("table_name", r.json()["msg"])
        self.assertEqual(r.json()["count"], 0)
        self.assertListEqual(r.json()["data"], [])

    @patch("sql_api.api_instance.resolve_table_instances")
    def test_lookup_table_instances_error(self, mock_resolve):
        """测试按表名查询异常时固定返回格式"""
        mock_resolve.side_effect = RuntimeError("boom")
        r = self.client.post(
            "/api/v1/instance/table-instances/",
            {"table_name": "orders"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.json()["status"], 1)
        self.assertIn("查询失败", r.json()["msg"])
        self.assertEqual(r.json()["count"], 0)
        self.assertListEqual(r.json()["data"], [])


class TestWorkflow(APITestCase):
    """测试工单相关接口"""

    def setUp(self):
        self.now = datetime.now()
        self.group = Group.objects.create(id=1, name="DBA")
        self.res_group = ResourceGroup.objects.create(group_id=1, group_name="test")
        self.ins_tag = InstanceTag.objects.create(tag_code="can_write", active=1)
        self.wfs = WorkflowAuditSetting.objects.create(
            group_id=self.res_group.group_id,
            workflow_type=2,
            audit_auth_groups=self.group.id,
        )
        can_submit = Permission.objects.get(codename="sql_submit")
        can_execute_permission = Permission.objects.get(codename="sql_execute")
        can_execute_resource_permission = Permission.objects.get(
            codename="sql_execute_for_resource_group"
        )
        can_review_permission = Permission.objects.get(codename="sql_review")
        can_query_all_instances = Permission.objects.get(codename="query_all_instances")
        self.user = User(
            username="test_user", display="测试用户", is_active=True, is_superuser=True
        )
        self.user.set_password("test_password")
        self.user.save()
        self.user.user_permissions.add(
            can_submit,
            can_execute_permission,
            can_execute_resource_permission,
            can_review_permission,
            can_query_all_instances,
        )
        self.user.groups.add(self.group)
        self.user.resource_group.add(self.res_group)
        self.ins = Instance.objects.create(
            instance_name="some_ins",
            type="master",
            db_type="mysql",
            host="some_host",
            port=3306,
            user="ins_user",
            password="some_str",
        )
        self.ins.resource_group.add(self.res_group)
        self.ins.instance_tag.add(self.ins_tag.id)
        self.wf1 = SqlWorkflow.objects.create(
            workflow_name="some_name",
            group_id=1,
            group_name="g1",
            engineer=self.user.username,
            engineer_display=self.user.display,
            audit_auth_groups="1",
            create_time=self.now - timedelta(days=1),
            status="workflow_manreviewing",
            is_backup=False,
            instance=self.ins,
            db_name="some_db",
            syntax_type=1,
        )
        self.wfc1 = SqlWorkflowContent.objects.create(
            workflow=self.wf1,
            sql_content="some_sql",
            execute_result=json.dumps([{"id": 1, "sql": "some_content"}]),
        )
        self.audit1 = WorkflowAudit.objects.create(
            group_id=1,
            group_name="some_group",
            workflow_id=self.wf1.id,
            workflow_type=2,
            workflow_title="申请标题",
            workflow_remark="申请备注",
            audit_auth_groups="1",
            current_audit="1",
            next_audit="-1",
            current_status=0,
            create_user=self.user.username,
            create_user_display=self.user.display,
        )
        self.wl = WorkflowLog.objects.create(
            audit_id=self.audit1.audit_id, operation_type=1
        )
        r = self.client.post(
            "/api/auth/token/",
            {"username": "test_user", "password": "test_password"},
            format="json",
        )
        self.token = r.data["access"]
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + self.token)
        SysConfig().set("api_user_whitelist", self.user.id)
        self.get_engine_patcher = patch("sql_api.serializers.get_engine")
        mock_get_engine = self.get_engine_patcher.start()
        mock_engine = MagicMock()
        mock_engine.auto_backup = False
        mock_engine.execute_check.return_value = ReviewSet(
            warning_count=0,
            error_count=0,
            column_list=["id", "stage"],
            rows=[
                ReviewResult(
                    id=1,
                    stage="CHECKED",
                    errlevel=0,
                    stagestatus="Audit Completed",
                    errormessage="",
                    sql="alter table abc add column note varchar(64);",
                    affected_rows=0,
                    actual_affected_rows=0,
                    sequence="0_0_00000000",
                    backup_dbname="",
                    execute_time="0",
                    sqlsha1="",
                )
            ],
        )
        mock_get_engine.return_value = mock_engine
        self.async_task_patcher = patch("sql_api.api_workflow.async_task")
        self.async_task_patcher.start()
        self.notify_patcher = patch("sql.notify.auto_notify")
        self.notify_patcher.start()

    def tearDown(self):
        self.user.delete()
        self.group.delete()
        self.res_group.delete()
        SqlWorkflowContent.objects.all().delete()
        SqlWorkflow.objects.all().delete()
        WorkflowAudit.objects.all().delete()
        WorkflowLog.objects.all().delete()
        self.get_engine_patcher.stop()
        self.async_task_patcher.stop()
        self.notify_patcher.stop()

    def test_get_sql_workflow_list(self):
        """测试获取SQL上线工单列表"""
        r = self.client.get("/api/v1/workflow/", format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.json()["count"], 1)

    def test_get_audit_list(self):
        """测试获取待审核工单列表"""
        json_data = {"engineer": "test_user"}
        r = self.client.post("/api/v1/workflow/auditlist/", json_data, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.json()["count"], 1)

    def test_get_workflow_log_list(self):
        """测试获工单日志"""
        json_data = {
            "workflow_id": self.wf1.id,
            "workflow_type": self.audit1.workflow_type,
        }
        r = self.client.post("/api/v1/workflow/log/", json_data, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.json()["count"], 1)

    def test_check_param_is_None(self):
        """测试工单检测，参数内容为空"""
        json_data = {
            "full_sql": "",
            "db_name": "test_db",
            "instance_id": self.ins.id,
        }
        r = self.client.post("/api/v1/workflow/sqlcheck/", json_data, format="json")
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    @patch("sql_api.api_workflow.get_engine")
    def test_check_inception_Exception(self, _get_engine):
        """测试工单检测，inception报错"""
        json_data = {
            "full_sql": "use mysql",
            "db_name": "test_db",
            "instance_id": self.ins.id,
        }
        _get_engine.side_effect = RuntimeError("RuntimeError")
        r = self.client.post("/api/v1/workflow/sqlcheck/", json_data, format="json")
        print(json.loads(r.content))
        self.assertDictEqual(json.loads(r.content), {"errors": "RuntimeError"})

    @patch("sql_api.api_workflow.get_engine")
    def test_check(self, _get_engine):
        """测试工单检测，正常返回"""
        json_data = {
            "full_sql": "use mysql",
            "db_name": "test_db",
            "instance_id": self.ins.id,
        }
        column_list = [
            "id",
            "stage",
            "errlevel",
            "stagestatus",
            "errormessage",
            "sql",
            "affected_rows",
            "sequence",
            "backup_dbname",
            "execute_time",
            "sqlsha1",
            "backup_time",
            "actual_affected_rows",
        ]

        rows = [
            ReviewResult(
                id=1,
                stage="CHECKED",
                errlevel=0,
                stagestatus="Audit Completed",
                errormessage="",
                sql="use `archer`",
                affected_rows=0,
                actual_affected_rows=0,
                sequence="0_0_00000000",
                backup_dbname="",
                execute_time="0",
                sqlsha1="",
            )
        ]
        _get_engine.return_value.execute_check.return_value = ReviewSet(
            warning_count=0, error_count=0, column_list=column_list, rows=rows
        )
        r = self.client.post("/api/v1/workflow/sqlcheck/", json_data, format="json")
        self.assertListEqual(
            list(json.loads(r.content).keys()),
            [
                "is_execute",
                "checked",
                "warning",
                "error",
                "warning_count",
                "error_count",
                "is_critical",
                "syntax_type",
                "rows",
                "column_list",
                "status",
                "affected_rows",
            ],
        )
        self.assertListEqual(list(json.loads(r.content)["rows"][0].keys()), column_list)

    def test_submit_workflow(self):
        """测试提交SQL上线工单"""
        json_data = {
            "workflow": {
                "workflow_name": "上线工单1",
                "demand_url": "test",
                "group_id": 1,
                "db_name": "test_db",
                "instance": self.ins.id,
                "is_offline_export": 0,
            },
            "sql_content": "alter table abc add column note varchar(64);",
        }
        r = self.client.post("/api/v1/workflow/", json_data, format="json")
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        self.assertEqual(r.json()["workflow"]["workflow_name"], "上线工单1")
        self.assertEqual(r.json()["workflow"]["engineer"], self.user.username)
        self.assertEqual(r.json()["workflow"]["engineer_display"], self.user.display)

    def test_submit_workflow_super(self):
        """测试管理员提交SQL上线工单，可以指定用户"""
        User.objects.filter(id=self.user.id).update(is_superuser=1)
        user2 = User.objects.create(
            username="test_user2", display="测试用户2", is_active=True
        )
        user2.groups.add(self.group)
        user2.resource_group.add(self.res_group)
        json_data = {
            "workflow": {
                "workflow_name": "上线工单1",
                "demand_url": "test",
                "group_id": 1,
                "db_name": "test_db",
                "engineer": "test_user2",
                "instance": self.ins.id,
                "is_offline_export": 0,
            },
            "sql_content": "alter table abc add column note varchar(64);",
        }
        r = self.client.post("/api/v1/workflow/", json_data, format="json")
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        self.assertEqual(r.json()["workflow"]["workflow_name"], "上线工单1")
        self.assertEqual(r.json()["workflow"]["engineer"], user2.username)
        self.assertEqual(r.json()["workflow"]["engineer_display"], user2.display)

    @patch("sql.utils.workflow_audit.AuditV2.generate_audit_setting")
    def test_submit_workflow_auto_pass(self, mock_generate_settings):
        json_data = {
            "workflow": {
                "workflow_name": "上线工单1",
                "demand_url": "test",
                "group_id": 1,
                "db_name": "test_db",
                "instance": self.ins.id,
                "is_offline_export": 0,
            },
            "sql_content": "alter table abc add column note varchar(64);",
        }
        mock_generate_settings.return_value = AuditSetting(auto_pass=True)
        r = self.client.post("/api/v1/workflow/", json_data, format="json")
        return_data = r.json()
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        workflow_in_db = SqlWorkflow.objects.get(id=return_data["workflow"]["id"])
        assert workflow_in_db.status == "workflow_review_pass"

    @patch("sql_api.serializers.OffLineDownLoad.pre_count_check")
    def test_submit_offline_export_uses_can_read_and_disables_backup(
        self, mock_pre_count_check
    ):
        """测试数据导出工单使用can_read权限并强制不备份"""
        can_read = InstanceTag.objects.create(
            tag_code="can_read", tag_name="支持查询", active=1
        )
        read_only_ins = Instance.objects.create(
            instance_name="read_only_ins",
            type="slave",
            db_type="redis",
            host="some_host",
            port=6379,
            user="ins_user",
            password="some_str",
        )
        read_only_ins.resource_group.add(self.res_group.group_id)
        read_only_ins.instance_tag.add(can_read.id)
        check_result = ReviewSet(rows=[ReviewResult(errlevel=0, affected_rows=1)])
        check_result.syntax_type = 3
        mock_pre_count_check.return_value = check_result
        json_data = {
            "workflow": {
                "workflow_name": "导出工单1",
                "group_id": 1,
                "db_name": "0",
                "instance": read_only_ins.id,
                "is_backup": True,
                "is_offline_export": 1,
                "export_format": "csv",
            },
            "sql_content": "get key1",
        }

        r = self.client.post("/api/v1/workflow/", json_data, format="json")

        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        workflow = SqlWorkflow.objects.get(id=r.json()["workflow"]["id"])
        self.assertEqual(workflow.syntax_type, 3)
        self.assertFalse(workflow.is_backup)

    def test_submit_workflow_still_requires_can_write(self):
        """测试SQL上线工单仍然要求can_write权限"""
        can_read = InstanceTag.objects.create(
            tag_code="can_read", tag_name="支持查询", active=1
        )
        read_only_ins = Instance.objects.create(
            instance_name="read_only_submit_ins",
            type="slave",
            db_type="redis",
            host="some_host",
            port=6379,
            user="ins_user",
            password="some_str",
        )
        read_only_ins.resource_group.add(self.res_group.group_id)
        read_only_ins.instance_tag.add(can_read.id)
        json_data = {
            "workflow": {
                "workflow_name": "上线工单1",
                "group_id": 1,
                "db_name": "0",
                "instance": read_only_ins.id,
                "is_offline_export": 0,
            },
            "sql_content": "set key1 value1",
        }

        r = self.client.post("/api/v1/workflow/", json_data, format="json")

        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("你所在组未关联该实例", r.json()["errors"])

    def test_submit_param_is_None(self):
        """测试SQL提交，参数内容为空"""
        json_data = {
            "workflow": {
                "workflow_name": "上线工单1",
                "demand_url": "test",
                "group_id": 1,
                "db_name": "test_db",
                "engineer": self.user.username,
                "instance": self.ins.id,
            },
            "sql_content": "",
        }
        r = self.client.post("/api/v1/workflow/", json_data, format="json")
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_audit_workflow(self):
        """测试审核工单"""
        json_data = {
            "engineer": self.user.username,
            "workflow_id": self.wf1.id,
            "audit_remark": "取消",
            "workflow_type": self.audit1.workflow_type,
            "audit_type": "cancel",
        }
        r = self.client.post("/api/v1/workflow/audit/", json_data, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.json(), {"msg": "canceled"})

    def test_execute_workflow(self):
        """测试执行工单"""
        # 先审核
        audit_data = {
            "engineer": self.user.username,
            "workflow_id": self.wf1.id,
            "audit_remark": "通过",
            "workflow_type": self.audit1.workflow_type,
            "audit_type": "pass",
        }
        self.client.post("/api/v1/workflow/audit/", audit_data, format="json")
        # 再执行
        execute_data = {
            "engineer": self.user.username,
            "workflow_id": self.wf1.id,
            "workflow_type": self.audit1.workflow_type,
            "mode": "manual",
        }
        r = self.client.post("/api/v1/workflow/execute/", execute_data, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.json(), {"msg": "开始执行，执行结果请到工单详情页查看"})


class TestSpaEndpoints(APITestCase):
    """SPA 重构新增接口冒烟测试（相关文档 / Dashboard 图表 / 慢查趋势 / 审核参数校验）"""

    def setUp(self) -> None:
        self.superuser = User.objects.create(
            username="super", display="超管", is_superuser=True
        )
        self.client.force_login(self.superuser)

    def tearDown(self) -> None:
        self.superuser.delete()

    def test_dbaprinciples_document(self):
        """相关文档：返回 docs/docs.md 原文 + 标题"""
        r = self.client.get("/api/v1/document/dbaprinciples/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        body = r.json()
        self.assertIn("content", body)
        self.assertIn("title", body)
        self.assertTrue(body["content"].lstrip().startswith("#"))

    def test_dashboard_charts(self):
        """Dashboard 图表数据：10 张图齐全"""
        with patch("sql_api.api_dashboard.ChartDao") as MockChartDao:
            dao = MockChartDao.return_value
            dao.get_date_list.return_value = ["2026-01-01", "2026-01-02"]
            # 各 by_* 方法返回 {rows: [(label, value), ...]}
            dao.workflow_by_date.return_value = {"rows": [("2026-01-01", 1)]}
            dao.workflow_by_group.return_value = {"rows": [("g1", 2)]}
            dao.syntax_type.return_value = {"rows": [("DDL", 1)]}
            dao.workflow_by_user.return_value = {"rows": [("u1", 1)]}
            dao.querylog_effect_row_by_date.return_value = {"rows": [("2026-01-01", 10)]}
            dao.querylog_count_by_date.return_value = {"rows": [("2026-01-01", 3)]}
            dao.querylog_effect_row_by_user.return_value = {"rows": [("u1", 10)]}
            dao.querylog_effect_row_by_db.return_value = {"rows": [("db1", 10)]}
            dao.slow_query_count_by_db_by_user.return_value = {"rows": [("db1", 1)]}
            dao.slow_query_count_by_db.return_value = {"rows": [("db1", 1)]}
            dao.query_sql_prod_bill.return_value = {"rows": [("p1", 1)]}
            r = self.client.get(
                "/api/v1/dashboard/charts/?start_date=2026-01-01&end_date=2026-01-02"
            )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        body = r.json()
        for key in ["bar1", "bar2", "bar3", "bar5", "pie1", "pie2", "pie3", "pie4", "pie5", "line1"]:
            self.assertIn(key, body)

    def test_dashboard_charts_bad_date(self):
        """Dashboard 图表：日期格式错误返回 400"""
        r = self.client.get(
            "/api/v1/dashboard/charts/?start_date=not-a-date&end_date=2026-01-02"
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_dashboard_charts_resilient_to_missing_table(self):
        """Dashboard 图表：某张图查表失败（如慢查表不存在）不影响其它图，整体仍 200"""
        with patch("sql_api.api_dashboard.ChartDao") as MockChartDao:
            dao = MockChartDao.return_value
            dao.get_date_list.return_value = ["2026-01-01"]
            dao.workflow_by_date.return_value = {"rows": [("2026-01-01", 1)]}
            dao.workflow_by_group.return_value = {"rows": [("g1", 2)]}
            dao.syntax_type.return_value = {"rows": [("DDL", 1)]}
            dao.workflow_by_user.return_value = {"rows": [("u1", 1)]}
            dao.querylog_effect_row_by_date.return_value = {"rows": [("2026-01-01", 10)]}
            dao.querylog_count_by_date.return_value = {"rows": [("2026-01-01", 3)]}
            dao.querylog_effect_row_by_user.return_value = {"rows": [("u1", 10)]}
            dao.querylog_effect_row_by_db.return_value = {"rows": [("db1", 10)]}
            # 慢查询表不存在 → 这两个方法抛异常
            dao.slow_query_count_by_db_by_user.side_effect = RuntimeError("Table doesn't exist")
            dao.slow_query_count_by_db.side_effect = RuntimeError("Table doesn't exist")
            dao.query_sql_prod_bill.return_value = {"rows": [("p1", 1)]}
            r = self.client.get(
                "/api/v1/dashboard/charts/?start_date=2026-01-01&end_date=2026-01-01"
            )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        body = r.json()
        # 慢查相关的 pie3/bar3 返回空，其它图正常
        self.assertEqual(body["pie3"], [])
        self.assertEqual(body["bar3"]["series"][0]["data"], [])
        self.assertEqual(len(body["pie1"]), 1)

    def test_slowquery_trend_missing_checksum(self):
        """慢查趋势：缺 checksum 返回 400"""
        r = self.client.get("/api/v1/slowquery/trend/")
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_slowquery_trend(self):
        """慢查趋势：正常返回双 series"""
        with patch("sql_api.api_slowlog.ChartDao") as MockChartDao:
            dao = MockChartDao.return_value
            dao.slow_query_review_history_by_cnt.return_value = {
                "rows": [(10, "2026-01-01"), (20, "2026-01-02")]
            }
            dao.slow_query_review_history_by_pct_95_time.return_value = {
                "rows": [(0.5, "2026-01-01"), (0.8, "2026-01-02")]
            }
            r = self.client.get(
                "/api/v1/slowquery/trend/?checksum=abc&instance_name=some_ins"
            )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        body = r.json()
        self.assertEqual(len(body["x"]), 2)
        self.assertEqual(len(body["series"]), 2)

    def test_slowquery_review_empty_sort_not_500(self):
        """回归：前端传空 sortName 时，自建 MySQL 慢查统计不应 500（曾抛 FieldError）。

        用真实表 + 真实数据走完整 ORM 链路，确保排序逻辑（order_by + 切片 +
        迭代）在真实 QuerySet 上不出错。MagicMock 方式测不出这类问题。
        """
        from sql.models import SlowQuery, SlowQueryHistory
        from django.db import connection

        ins = Instance.objects.create(
            instance_name="mysql_local",
            type="master",
            db_type="mysql",
            host="127.0.0.1",
            port=3306,
        )
        rg = ResourceGroup.objects.create(group_name="g1")
        ins.resource_group.add(rg)
        self._create_slow_query_tables(connection)

        SlowQuery.objects.create(
            checksum="a" * 32,
            fingerprint="select sleep(?)",
            sample="SELECT SLEEP(1)",
            first_seen="2026-07-08 00:00:00",
            last_seen="2026-07-08 00:00:00",
        )
        SlowQueryHistory.objects.create(
            hostname_max="127.0.0.1:3306",
            user_max="root",
            db_max="archery",
            checksum=SlowQuery.objects.get(checksum="a" * 32),
            sample="SELECT SLEEP(1)",
            ts_min="2026-07-08 00:00:00",
            ts_max="2026-07-08 00:00:00",
            ts_cnt=1,
            query_time_sum=1.0,
            query_time_pct_95=1.0,
            lock_time_sum=0.0,
            rows_examined_sum=0,
            rows_sent_sum=0,
        )

        # 关键：不走阿里云分支
        with patch("sql_api.api_slowquery.AliyunRdsConfig.objects") as mock_rds:
            mock_rds.filter.return_value.exists.return_value = False
            r = self.client.post(
                "/api/v1/slowquery/review/",
                {
                    "instance_name": "mysql_local",
                    "db_name": "",
                    "StartTime": "",
                    "EndTime": "",
                    "limit": 1000,
                    "offset": 0,
                    "search": "",
                    "sortName": "",
                    "sortOrder": "",
                },
                format="json",
            )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        body = r.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(len(body["rows"]), 1)
        self.assertEqual(body["rows"][0]["SQLId"], "a" * 32)

    def test_slowquery_review_history_empty_sort_not_500(self):
        """回归：前端传空 sortName 时，自建 MySQL 慢查明细不应 500。

        此用例曾因 _apply_sort 返回 list（QuerySet 布尔求值触发缓存）而非
        QuerySet，导致后续 .values() 调用报 AttributeError。
        """
        from sql.models import SlowQuery, SlowQueryHistory
        from django.db import connection

        ins = Instance.objects.create(
            instance_name="mysql_local2",
            type="master",
            db_type="mysql",
            host="127.0.0.1",
            port=3306,
        )
        rg = ResourceGroup.objects.create(group_name="g2")
        ins.resource_group.add(rg)
        self._create_slow_query_tables(connection)

        SlowQuery.objects.create(
            checksum="b" * 32,
            fingerprint="select sleep(?)",
            sample="SELECT SLEEP(2)",
            first_seen="2026-07-08 00:00:00",
            last_seen="2026-07-08 00:00:00",
        )
        SlowQueryHistory.objects.create(
            hostname_max="127.0.0.1:3306",
            user_max="root",
            db_max="archery",
            checksum=SlowQuery.objects.get(checksum="b" * 32),
            sample="SELECT SLEEP(2)",
            ts_min="2026-07-08 00:00:00",
            ts_max="2026-07-08 00:00:00",
            ts_cnt=1,
            query_time_sum=2.0,
            query_time_pct_95=2.0,
            lock_time_sum=0.0,
            rows_examined_sum=0,
            rows_sent_sum=0,
        )

        with patch("sql_api.api_slowquery.AliyunRdsConfig.objects") as mock_rds:
            mock_rds.filter.return_value.exists.return_value = False
            r = self.client.post(
                "/api/v1/slowquery/review_history/",
                {
                    "instance_name": "mysql_local2",
                    "db_name": "",
                    "StartTime": "",
                    "EndTime": "",
                    "SQLId": "",
                    "limit": 1000,
                    "offset": 0,
                    "search": "",
                    "sortName": "",
                    "sortOrder": "",
                },
                format="json",
            )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        body = r.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(len(body["rows"]), 1)
        self.assertEqual(body["rows"][0]["SQLText"], "SELECT SLEEP(2)")

    @staticmethod
    def _create_slow_query_tables(connection):
        """建 managed=False 的慢查表（Django 迁移不会自动建）"""
        with connection.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS `mysql_slow_query_review` (
                  `checksum` CHAR(32) NOT NULL PRIMARY KEY,
                  `fingerprint` longtext NOT NULL,
                  `sample` longtext NOT NULL,
                  `first_seen` datetime(6) DEFAULT NULL,
                  `last_seen` datetime(6) DEFAULT NULL,
                  `reviewed_by` varchar(20) DEFAULT NULL,
                  `reviewed_on` datetime(6) DEFAULT NULL,
                  `comments` longtext
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS `mysql_slow_query_review_history` (
                  `id` int(11) NOT NULL AUTO_INCREMENT,
                  `hostname_max` varchar(64) NOT NULL,
                  `client_max` varchar(64) DEFAULT NULL,
                  `user_max` varchar(64) NOT NULL,
                  `db_max` varchar(64) DEFAULT NULL,
                  `checksum` CHAR(32) NOT NULL,
                  `sample` longtext NOT NULL,
                  `ts_min` datetime(6) NOT NULL,
                  `ts_max` datetime(6) NOT NULL,
                  `ts_cnt` float DEFAULT NULL,
                  `Query_time_sum` float DEFAULT NULL,
                  `Query_time_pct_95` float DEFAULT NULL,
                  `Lock_time_sum` float DEFAULT NULL,
                  `Rows_examined_sum` float DEFAULT NULL,
                  `Rows_sent_sum` float DEFAULT NULL,
                  PRIMARY KEY (`id`),
                  KEY `idx_hostname_max_ts_min` (`hostname_max`,`ts_min`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8
                """
            )

    def test_query_priv_audit_param_error(self):
        """查询权限审核：参数缺失返回 status=1"""
        r = self.client.post("/api/v1/query_priv/audit/", {}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.json()["status"], 1)

    def test_archive_audit_param_error(self):
        """归档审核：参数缺失返回 status=1"""
        r = self.client.post("/api/v1/archive/audit/", {}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.json()["status"], 1)

    def test_instance_tags_list(self):
        """实例标签清单"""
        from sql.models import InstanceTag

        InstanceTag.objects.create(tag_code="can_read", tag_name="支持查询", active=1)
        InstanceTag.objects.create(tag_code="inactive", tag_name="停用", active=0)
        r = self.client.get("/api/v1/instance/tags/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        codes = [t["tag_code"] for t in r.json()]
        self.assertIn("can_read", codes)
        self.assertNotIn("inactive", codes)  # 仅激活

    def test_permissions_list(self):
        """权限清单按模型分组"""
        r = self.client.get("/api/v1/user/permissions/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        body = r.json()
        self.assertIsInstance(body, list)
        self.assertTrue(all("permissions" in g for g in body))

    def test_aliyun_rds_detail_flow(self):
        """AliyunRDS 新接口：by_instance 查 + 详情改/删（create 已由 test_create_aliyunrds 覆盖）"""
        from sql.models import CloudAccessKey, AliyunRdsConfig

        ins = Instance.objects.create(
            instance_name="rds_ins", type="master", db_type="mysql",
            host="h", port=3306, user="u", password="p",
        )
        # 无配置 → by_instance 404
        r = self.client.get(f"/api/v1/instance/rds/by_instance/?instance={ins.id}")
        self.assertEqual(r.status_code, 404)
        # 直接建配置（create 接口另有 test_create_aliyunrds 覆盖）
        ak = CloudAccessKey.objects.create(type="aliyun", key_id="kid", key_secret="ksec")
        rds = AliyunRdsConfig.objects.create(
            rds_dbinstanceid="rm-xxx", is_enable=True, instance=ins, ak=ak
        )
        # by_instance 查得到
        r = self.client.get(f"/api/v1/instance/rds/by_instance/?instance={ins.id}")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.json()["rds_dbinstanceid"], "rm-xxx")
        # 更新
        up = self.client.put(
            f"/api/v1/instance/rds/{rds.id}/",
            {"rds_dbinstanceid": "rm-yyy", "is_enable": False,
             "ak": {"key_id": "kid2"}},
            format="json",
        )
        self.assertEqual(up.status_code, status.HTTP_200_OK)
        self.assertEqual(up.json()["rds_dbinstanceid"], "rm-yyy")
        # 删除
        d = self.client.delete(f"/api/v1/instance/rds/{rds.id}/")
        self.assertEqual(d.status_code, status.HTTP_204_NO_CONTENT)


class AiReviewParserTest(TestCase):
    """OpenaiClient._parse_review_json 与 review_sql_by_openai 容错测试。"""

    def test_parse_normal_json(self):
        from common.utils.openai import OpenaiClient

        data = OpenaiClient._parse_review_json(
            '{"risk_level": "high", "risk_score": 85, '
            '"summary": "大表加索引", "suggestion": "建议走OSC"}'
        )
        self.assertEqual(data["risk_level"], "high")
        self.assertEqual(data["risk_score"], 85)
        self.assertEqual(data["summary"], "大表加索引")

    def test_parse_json_wrapped_in_codeblock(self):
        from common.utils.openai import OpenaiClient

        data = OpenaiClient._parse_review_json(
            '```json\n{"risk_level": "low", "risk_score": 20, '
            '"summary": "ok", "suggestion": ""}\n```'
        )
        self.assertEqual(data["risk_level"], "low")
        self.assertEqual(data["risk_score"], 20)

    def test_parse_json_with_extra_text(self):
        from common.utils.openai import OpenaiClient

        data = OpenaiClient._parse_review_json(
            '审核结果如下：{"risk_level": "medium", "risk_score": 55, '
            '"summary": "中风险", "suggestion": "..."} 希望有帮助'
        )
        self.assertEqual(data["risk_level"], "medium")
        self.assertEqual(data["risk_score"], 55)

    def test_parse_invalid_returns_unknown(self):
        from common.utils.openai import OpenaiClient, AI_RISK_UNKNOWN

        self.assertEqual(
            OpenaiClient._parse_review_json("not a json at all")["risk_level"],
            AI_RISK_UNKNOWN,
        )
        self.assertEqual(
            OpenaiClient._parse_review_json("")["risk_level"], AI_RISK_UNKNOWN
        )

    def test_parse_normalizes_invalid_level_and_score(self):
        from common.utils.openai import OpenaiClient, AI_RISK_UNKNOWN

        data = OpenaiClient._parse_review_json(
            '{"risk_level": "weird", "risk_score": "abc", "summary": "x"}'
        )
        self.assertEqual(data["risk_level"], AI_RISK_UNKNOWN)
        self.assertEqual(data["risk_score"], 0)

    def test_review_sql_returns_fallback_on_exception(self):
        """review_sql_by_openai 内部异常时应返回 unknown，不抛出。"""
        from common.utils.openai import OpenaiClient, AI_RISK_UNKNOWN

        client = OpenaiClient.__new__(OpenaiClient)  # 跳过 __init__（不读配置）
        with patch.object(
            client,
            "request_chat_completion",
            side_effect=Exception("network error"),
        ):
            result = client.review_sql_by_openai(
                db_type="mysql",
                db_name="test",
                sql_text="select 1",
                table_schemas="(无)",
                table_rows="t: 1 行",
            )
        self.assertEqual(result["risk_level"], AI_RISK_UNKNOWN)

    def test_parse_trailing_comma(self):
        """尾部逗号容错（}, ] 前的逗号）。"""
        from common.utils.openai import OpenaiClient

        data = OpenaiClient._parse_review_json(
            '{"risk_level": "high", "risk_score": 80,}'
        )
        self.assertEqual(data["risk_level"], "high")
        self.assertEqual(data["risk_score"], 80)

    def test_parse_single_quotes(self):
        """单引号 → 双引号容错。"""
        from common.utils.openai import OpenaiClient

        data = OpenaiClient._parse_review_json(
            "{'risk_level': 'low', 'risk_score': 30}"
        )
        self.assertEqual(data["risk_level"], "low")
        self.assertEqual(data["risk_score"], 30)

    def test_parse_chinese_punctuation(self):
        """中文全角标点（，：“”）→ ASCII 容错。"""
        from common.utils.openai import OpenaiClient

        data = OpenaiClient._parse_review_json(
            '{"risk_level"\uff1a"high"\uff0c"risk_score"\uff1a88}'
        )
        self.assertEqual(data["risk_level"], "high")
        self.assertEqual(data["risk_score"], 88)

    def test_parse_preserves_apostrophe_in_string(self):
        """字符串内部的英文撇号不应被误转（it's 不会被破坏）。"""
        from common.utils.openai import OpenaiClient

        data = OpenaiClient._parse_review_json(
            '{"risk_level": "low", "risk_score": 10, "summary": "it\'s ok"}'
        )
        self.assertEqual(data["risk_level"], "low")
        self.assertIn("it", data["summary"])

    def test_parse_ddl_lock_fields(self):
        """变更影响预测字段（ddl_lock_risk/affected_rows_estimate/use_osc）解析。"""
        from common.utils.openai import OpenaiClient

        data = OpenaiClient._parse_review_json(
            '{"risk_level": "high", "risk_score": 85, '
            '"ddl_lock_risk": "high", "affected_rows_estimate": "约132万行", '
            '"use_osc": true}'
        )
        self.assertEqual(data["ddl_lock_risk"], "high")
        self.assertEqual(data["affected_rows_estimate"], "约132万行")
        self.assertIs(data["use_osc"], True)

    def test_parse_use_osc_string_coercion(self):
        """use_osc 字符串 'true' 应被转为 bool True。"""
        from common.utils.openai import OpenaiClient

        data = OpenaiClient._parse_review_json(
            '{"risk_level": "high", "risk_score": 85, '
            '"ddl_lock_risk": "high", "use_osc": "true"}'
        )
        self.assertIs(data["use_osc"], True)

    def test_fallback_includes_ddl_fields(self):
        """容错降级返回应包含新增的 DDL 字段，保证字段一致性。"""
        from common.utils.openai import OpenaiClient, AI_REVIEW_FALLBACK

        # 非法输入 → fallback
        data = OpenaiClient._parse_review_json("totally not json")
        for key in AI_REVIEW_FALLBACK:
            self.assertIn(key, data)
        self.assertEqual(data["ddl_lock_risk"], "none")
        self.assertIs(data["use_osc"], False)


class AiRiskSummaryTest(TestCase):
    """WorkflowDetail._calc_ai_risk_summary 汇总逻辑测试。"""

    def _summary(self, review_content):
        from sql_api.api_workflow import WorkflowDetail

        return WorkflowDetail._calc_ai_risk_summary(review_content)

    def test_empty_content_returns_fallback(self):
        s = self._summary(None)
        self.assertEqual(s["ai_max_risk_level"], "")
        self.assertEqual(s["ai_max_risk_score"], 0)

    def test_no_ai_data_returns_fallback(self):
        s = self._summary(json.dumps([{"sql": "select 1", "errlevel": 0}]))
        self.assertEqual(s["ai_max_risk_level"], "")

    def test_takes_max_score(self):
        rows = [
            {"sql": "a", "ai_risk_level": "low", "ai_risk_score": 20},
            {"sql": "b", "ai_risk_level": "high", "ai_risk_score": 90},
            {"sql": "c", "ai_risk_level": "medium", "ai_risk_score": 55},
        ]
        s = self._summary(json.dumps(rows))
        self.assertEqual(s["ai_max_risk_level"], "high")
        self.assertEqual(s["ai_max_risk_score"], 90)
        self.assertEqual(s["ai_high_risk_count"], 1)

    def test_counts_multiple_high_risk(self):
        rows = [
            {"sql": "a", "ai_risk_level": "high", "ai_risk_score": 80},
            {"sql": "b", "ai_risk_level": "high", "ai_risk_score": 95},
        ]
        s = self._summary(json.dumps(rows))
        self.assertEqual(s["ai_high_risk_count"], 2)
        self.assertEqual(s["ai_max_risk_score"], 95)

    def test_counts_lock_high_risk(self):
        """汇总应统计 DDL 锁表高危（ai_ddl_lock_risk=high）的条数。"""
        rows = [
            {"sql": "ddl1", "ai_risk_level": "high", "ai_risk_score": 85,
             "ai_ddl_lock_risk": "high"},
            {"sql": "ddl2", "ai_risk_level": "high", "ai_risk_score": 80,
             "ai_ddl_lock_risk": "medium"},
            {"sql": "dml1", "ai_risk_level": "medium", "ai_risk_score": 55,
             "ai_ddl_lock_risk": "none"},
        ]
        s = self._summary(json.dumps(rows))
        self.assertEqual(s["ai_lock_high_count"], 1)
        self.assertEqual(s["ai_high_risk_count"], 2)


class ExecuteCheckAiIntegrationTest(TestCase):
    """MysqlEngine._ai_review_check 开关与降级行为测试（mock OpenAI，不连库）。"""

    def _make_engine(self):
        from sql.engines.mysql import MysqlEngine

        engine = MysqlEngine.__new__(MysqlEngine)  # 跳过 __init__
        engine.config = MagicMock()
        engine.config.get = lambda k, d=False: d
        engine.escape_string = lambda v: v
        engine.describe_table = MagicMock()
        engine.get_table_meta_data = MagicMock()
        return engine

    def _make_reviewset(self, sqls):
        from sql.engines.models import ReviewResult, ReviewSet

        rows = [ReviewResult(id=i + 1, sql=s) for i, s in enumerate(sqls)]
        rs = ReviewSet(rows=rows)
        return rs

    def test_disabled_switch_skips_ai(self):
        """开关关闭时（默认）不调用 AI，rows 不带 ai 字段。"""
        engine = self._make_engine()
        rs = self._make_reviewset(["update t set a=1"])
        with patch("common.utils.openai.check_openai_config") as mock_check:
            engine._ai_review_check(rs, "test")
            mock_check.assert_not_called()
        self.assertFalse(hasattr(rs.rows[0], "ai_risk_level"))

    def test_no_api_key_skips_ai(self):
        """开关开启但 API Key 缺失时静默跳过。"""
        engine = self._make_engine()
        engine.config.get = lambda k, d=False: True if k == "ai_review_enabled" else d
        rs = self._make_reviewset(["update t set a=1"])
        with patch(
            "common.utils.openai.check_openai_config", return_value=False
        ), patch("common.utils.openai.OpenaiClient") as mock_client:
            engine._ai_review_check(rs, "test")
            mock_client.assert_not_called()
        self.assertFalse(hasattr(rs.rows[0], "ai_risk_level"))

    def test_ai_failure_marks_unknown(self):
        """开关+Key 均就绪但 AI 批量与单条回退均失败时，该条标记 unknown，不中断。"""
        from common.utils.openai import AI_RISK_UNKNOWN

        engine = self._make_engine()
        engine.config.get = lambda k, d=False: True if k == "ai_review_enabled" else d
        rs = self._make_reviewset(["update t set a=1"])
        mock_client = MagicMock()
        mock_client.review_sql_batch_by_openai.return_value = None
        mock_client.review_sql_by_openai.side_effect = Exception("AI boom")
        with patch(
            "common.utils.openai.check_openai_config", return_value=True
        ), patch(
            "common.utils.openai.OpenaiClient", return_value=mock_client
        ), patch(
            "sql.utils.ai_review.extract_tables", return_value=[]
        ):
            engine._ai_review_check(rs, "test")
        self.assertEqual(rs.rows[0].ai_risk_level, AI_RISK_UNKNOWN)
        self.assertEqual(rs.rows[0].ai_summary, "AI 审核失败")

    def test_ai_success_attaches_fields(self):
        """AI 批量正常返回时，row 挂上 ai_* 字段（含变更影响预测）。"""
        engine = self._make_engine()
        engine.config.get = lambda k, d=False: True if k == "ai_review_enabled" else d
        rs = self._make_reviewset(["alter table t add index idx_a(a)"])
        mock_client = MagicMock()
        mock_client.review_sql_batch_by_openai.return_value = [{
            "risk_level": "high",
            "risk_score": 88,
            "summary": "大表加索引将长时间锁表",
            "suggestion": "建议走 gh-ost",
            "ddl_lock_risk": "high",
            "affected_rows_estimate": "约132万行",
            "use_osc": True,
        }]
        with patch(
            "common.utils.openai.check_openai_config", return_value=True
        ), patch(
            "common.utils.openai.OpenaiClient", return_value=mock_client
        ), patch(
            "sql.utils.ai_review.extract_tables", return_value=[{"name": "t"}]
        ), patch(
            "sql.utils.ai_review._collect_context", return_value=("(ddl)", "(rows)")
        ):
            engine._ai_review_check(rs, "test")
        self.assertEqual(rs.rows[0].ai_risk_level, "high")
        self.assertEqual(rs.rows[0].ai_risk_score, 88)
        self.assertEqual(rs.rows[0].ai_summary, "大表加索引将长时间锁表")
        # 变更影响预测字段
        self.assertEqual(rs.rows[0].ai_ddl_lock_risk, "high")
        self.assertEqual(rs.rows[0].ai_affected_rows_estimate, "约132万行")
        self.assertIs(rs.rows[0].ai_use_osc, True)

    def test_ai_over_limit_marks_unknown_with_ddl_fields(self):
        """超过审核条数上限的 row 也应挂上 DDL 默认字段，保证字段一致。"""
        engine = self._make_engine()
        engine.config.get = lambda k, d=False: True if k == "ai_review_enabled" else d
        # 构造超过 AI_REVIEW_MAX_STATEMENTS(20) 的 reviewset
        sqls = [f"update t set a={i}" for i in range(25)]
        rs = self._make_reviewset(sqls)
        mock_client = MagicMock()
        mock_client.review_sql_batch_by_openai.return_value = []
        with patch(
            "common.utils.openai.check_openai_config", return_value=True
        ), patch(
            "common.utils.openai.OpenaiClient", return_value=mock_client
        ), patch(
            "sql.utils.ai_review.extract_tables", return_value=[]
        ):
            engine._ai_review_check(rs, "test")
            # 前 20 条参与 AI，按批量大小分 2 批；第 21+ 条跳过
            self.assertEqual(mock_client.review_sql_batch_by_openai.call_count, 2)
        # 第 21 条（idx=20）标记 unknown + DDL 默认字段
        over_row = rs.rows[20]
        from common.utils.openai import AI_RISK_UNKNOWN

        self.assertEqual(over_row.ai_risk_level, AI_RISK_UNKNOWN)
        self.assertEqual(over_row.ai_ddl_lock_risk, "none")
        self.assertIs(over_row.ai_use_osc, False)


class AiReviewMaskingAndUsageTest(TestCase):
    """_ai_review_check 外发脱敏（H4 回灌口径统一）+ AIUsageLog 用量记账"""

    def _make_engine(self):
        from sql.engines.mysql import MysqlEngine

        engine = MysqlEngine.__new__(MysqlEngine)  # 跳过 __init__
        engine.config = MagicMock()
        engine.config.get = lambda k, d=False: d
        engine.escape_string = lambda v: v
        engine.describe_table = MagicMock()
        engine.get_table_meta_data = MagicMock()
        return engine

    def _make_reviewset(self, sqls):
        from sql.engines.models import ReviewResult, ReviewSet

        rows = [ReviewResult(id=i + 1, sql=s) for i, s in enumerate(sqls)]
        return ReviewSet(rows=rows)

    def test_sql_and_ddl_masked_before_send(self):
        """待审核 SQL 与 DDL 上下文外发前均做字面量脱敏（批量链路）。"""
        engine = self._make_engine()
        engine.config.get = lambda k, d=False: True if k == "ai_review_enabled" else d
        rs = self._make_reviewset(["update t set phone='13800138000' where id=999"])
        captured = {}
        valid_result = {
            "risk_level": "low", "risk_score": 10, "summary": "ok",
            "suggestion": "", "ddl_lock_risk": "none",
            "affected_rows_estimate": "", "use_osc": False,
        }
        mock_client = MagicMock()

        def fake_batch(**kwargs):
            captured.update(kwargs)
            return [dict(valid_result)]

        mock_client.review_sql_batch_by_openai.side_effect = fake_batch
        with patch(
            "common.utils.openai.check_openai_config", return_value=True
        ), patch(
            "common.utils.openai.OpenaiClient", return_value=mock_client
        ), patch(
            "sql.utils.ai_review.extract_tables", return_value=[{"name": "t"}]
        ), patch(
            "sql.utils.ai_review._collect_context",
            return_value=("CREATE TABLE t (c varchar(10) COMMENT '真实手机号')", "t: 约 100 行"),
        ):
            engine._ai_review_check(rs, "test", user_name="alice")
        # SQL 字面量不外发：字符串→'?'，数字→0
        self.assertEqual(len(captured["statements"]), 1)
        self.assertNotIn("13800138000", captured["statements"][0])
        self.assertIn("'?'", captured["statements"][0])
        # DDL 注释中的疑似业务数据不外发
        self.assertNotIn("真实手机号", captured["table_schemas"])
        # 用量记账带用户归属（批量按批记一条）
        log = AIUsageLog.objects.get(capability="sql_review")
        self.assertEqual(log.user_name, "alice")
        self.assertEqual(log.status, "success")
        self.assertEqual(log.instance_name, "")

    def test_failure_records_failed_usage(self):
        """批量与单条回退均失败时记 failed 用量，且不影响降级兜底。"""
        from common.utils.openai import AI_RISK_UNKNOWN

        engine = self._make_engine()
        engine.config.get = lambda k, d=False: True if k == "ai_review_enabled" else d
        rs = self._make_reviewset(["update t set a=1"])
        mock_client = MagicMock()
        mock_client.review_sql_batch_by_openai.return_value = None
        mock_client.review_sql_by_openai.side_effect = Exception("AI boom")
        with patch(
            "common.utils.openai.check_openai_config", return_value=True
        ), patch(
            "common.utils.openai.OpenaiClient", return_value=mock_client
        ), patch(
            "sql.utils.ai_review.extract_tables", return_value=[]
        ):
            engine._ai_review_check(rs, "test")
        self.assertEqual(rs.rows[0].ai_risk_level, AI_RISK_UNKNOWN)
        log = AIUsageLog.objects.filter(capability="sql_review").order_by("-id").first()
        self.assertEqual(log.status, "failed")
        self.assertIn("AI boom", log.error)


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
class AiReviewCacheTest(TestCase):
    """_ai_review_check 结果缓存：重复检测同一 SQL 不再调用 AI，命中也记账"""

    def setUp(self):
        # LocMemCache 在测试间不自动清空，显式清理避免用例间缓存串扰
        from django.core.cache import cache

        cache.clear()

    def _make_engine(self):
        from sql.engines.mysql import MysqlEngine

        engine = MysqlEngine.__new__(MysqlEngine)  # 跳过 __init__
        engine.config = MagicMock()
        engine.config.get = lambda k, d=False: d
        engine.escape_string = lambda v: v
        engine.describe_table = MagicMock()
        engine.get_table_meta_data = MagicMock()
        return engine

    def _make_reviewset(self, sqls):
        from sql.engines.models import ReviewResult, ReviewSet

        rows = [ReviewResult(id=i + 1, sql=s) for i, s in enumerate(sqls)]
        return ReviewSet(rows=rows)

    def test_repeat_check_hits_cache(self):
        """同批 SQL 第二次检测命中批级缓存：AI 只调用一次，字段照常挂载并记 cache_hit。"""
        engine = self._make_engine()
        engine.config.get = lambda k, d=False: True if k == "ai_review_enabled" else d
        review_result = {
            "risk_level": "high", "risk_score": 88, "summary": "大表加索引将长时间锁表",
            "suggestion": "建议走 gh-ost", "ddl_lock_risk": "high",
            "affected_rows_estimate": "约132万行", "use_osc": True,
        }
        mock_client = MagicMock()
        mock_client.review_sql_batch_by_openai.return_value = [dict(review_result)]
        with patch(
            "common.utils.openai.check_openai_config", return_value=True
        ), patch(
            "common.utils.openai.OpenaiClient", return_value=mock_client
        ), patch(
            "sql.utils.ai_review.extract_tables", return_value=[{"name": "t"}]
        ), patch(
            "sql.utils.ai_review._collect_context", return_value=("ddl", "rows")
        ):
            rs1 = self._make_reviewset(["update t set a=1 where id=999"])
            engine._ai_review_check(rs1, "test", user_name="u1")
            rs2 = self._make_reviewset(["update t set a=1 where id=999"])
            engine._ai_review_check(rs2, "test", user_name="u2")
        # 第二次命中批级缓存，AI 只被真实调用一次
        self.assertEqual(mock_client.review_sql_batch_by_openai.call_count, 1)
        self.assertEqual(rs2.rows[0].ai_risk_level, "high")
        self.assertIs(rs2.rows[0].ai_use_osc, True)
        logs = list(AIUsageLog.objects.filter(capability="sql_review").order_by("id"))
        self.assertEqual(len(logs), 2)
        self.assertFalse(logs[0].cache_hit)
        self.assertTrue(logs[1].cache_hit)
        self.assertEqual(logs[1].prompt_tokens, 0)
        self.assertEqual(logs[1].user_name, "u2")


class AiUsageApiTest(APITestCase):
    """AI 用量查询接口：仅超管可访问，聚合与筛选正确"""

    def setUp(self):
        self.superuser = User.objects.create(
            username="usage_admin", display="管理", is_active=True, is_superuser=True
        )
        self.superuser.set_password("test_password")
        self.superuser.save()
        self.normal_user = User.objects.create(
            username="usage_normal", display="普通", is_active=True
        )
        self.normal_user.set_password("test_password")
        self.normal_user.save()
        AIUsageLog.objects.create(
            capability="sql_review", model="m1", db_type="mysql",
            instance_name="ins1", db_name="db1", user_name="alice",
            prompt_tokens=100, completion_tokens=20, latency_ms=1500,
        )
        AIUsageLog.objects.create(
            capability="sql_optimize", model="m1", db_type="mysql",
            instance_name="ins1", db_name="db1", user_name="bob",
            prompt_tokens=50, completion_tokens=10, latency_ms=2500,
            cache_hit=True,
        )

    def test_summary_aggregates(self):
        self.client.force_login(self.superuser)
        resp = self.client.get("/api/v1/ai_usage/summary/", {"days": 7})
        data = resp.json()
        self.assertEqual(data["totals"]["calls"], 2)
        self.assertEqual(data["totals"]["cache_hits"], 1)
        self.assertEqual(data["totals"]["prompt_tokens"], 150)
        caps = {row["capability"]: row for row in data["by_capability"]}
        self.assertEqual(caps["sql_review"]["calls"], 1)
        self.assertEqual(caps["sql_review"]["cache_hits"], 0)
        self.assertEqual(data["by_day"][0]["calls"], 2)
        users = {row["user_name"]: row for row in data["by_user"]}
        self.assertEqual(users["alice"]["prompt_tokens"], 100)

    def test_list_filter(self):
        self.client.force_login(self.superuser)
        resp = self.client.get(
            "/api/v1/ai_usage/list/", {"capability": "sql_review"}
        )
        data = resp.json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["rows"][0]["user_name"], "alice")

    def test_forbidden_for_normal_user(self):
        self.client.force_login(self.normal_user)
        resp = self.client.get("/api/v1/ai_usage/summary/")
        self.assertEqual(resp.status_code, 403)
        resp = self.client.get("/api/v1/ai_usage/list/")
        self.assertEqual(resp.status_code, 403)


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
class OptimizeAIAsyncTest(APITestCase):
    """AI 优化异步任务：提交/轮询/缓存回填/去重/越权防护"""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.superuser = User.objects.create(
            username="async_admin", display="管理", is_active=True, is_superuser=True
        )
        self.superuser.set_password("test_password")
        self.superuser.save()
        self.other_user = User.objects.create(
            username="async_other", display="他人", is_active=True
        )
        self.other_user.set_password("test_password")
        self.other_user.save()
        # 越权用户需持有优化工具菜单权限，确保被拒发生在"任务归属"校验
        # 而非视图权限类（DRF 403）
        from django.contrib.auth.models import Permission

        self.other_user.user_permissions.add(
            Permission.objects.get(codename="menu_sqladvisor")
        )
        res_group = ResourceGroup.objects.create(group_name="async_g1")
        self.ins = Instance.objects.create(
            instance_name="async_ins", type="master", db_type="mysql",
            host="some_host", port=3306, user="ins_user", password="some_str",
        )
        self.ins.resource_group.add(res_group)
        self.superuser.resource_group.add(res_group)

    def _mock_client(self):
        client = MagicMock()
        client.total_usage = {"prompt_tokens": 10, "completion_tokens": 5}
        client.total_latency_ms = 123
        client.default_chat_model = "model-a"
        return client

    def _patch_ai(self, mock_client, executor):
        return patch(
            "sql_api.api_slowquery.check_openai_config", return_value=True
        ), patch(
            "sql_api.api_slowquery.SysConfig"
        ), patch(
            "sql_api.api_slowquery.OpenaiClient", return_value=mock_client
        ), patch(
            "sql_api.api_slowquery.run_agent_optimize",
            return_value=("# 优化报告", [{"tool": "get_table_ddl", "args": {}, "ok": True, "elapsed": 0.1}]),
        ), patch(
            "sql_api.api_slowquery._get_optimize_executor", return_value=executor
        )

    def _post_submit(self):
        return self.client.post("/api/v1/optimize/ai/async/", {
            "instance_name": "async_ins",
            "db_name": "db1",
            "sql_content": "select * from t1 where id = 1",
        })

    def test_submit_run_poll_and_cache_backfill(self):
        """提交 → 同步执行任务体 → 轮询取报告；重复提交命中回填缓存。

        不用内联执行器跑 `_run`：其 finally 中的 connections.close_all()
        会破坏 TestCase 的事务连接，改为提交后直接调 run_optimize_task。
        """
        import threading
        from sql.models import AIOptimizeTask
        from sql_api.api_slowquery import run_optimize_task

        class NonRunningExecutor:
            def submit(self, fn):
                pass

        mock_client = self._mock_client()
        p1, p2, p3, p4, p5 = self._patch_ai(
            mock_client, (NonRunningExecutor(), threading.Semaphore(99))
        )
        self.client.force_login(self.superuser)
        with p1, p2 as mock_cfg, p3, p4, p5:
            mock_cfg.return_value.get.return_value = "model-a"
            resp = self._post_submit()
            self.assertEqual(resp.json()["status"], 0)
            task_id = resp.json()["data"]["task_id"]
            self.assertEqual(AIOptimizeTask.objects.get(id=task_id).status, "pending")

            # 同步执行任务体（等价于线程池内的执行，patch 仍然生效）
            run_optimize_task(task_id)

            task = AIOptimizeTask.objects.get(id=task_id)
            self.assertEqual(task.status, "success")
            self.assertEqual(task.report_markdown, "# 优化报告")
            self.assertEqual(task.prompt_tokens, 10)

            # 轮询接口
            resp = self.client.get(f"/api/v1/optimize/ai/async/{task_id}/")
            data = resp.json()["data"]
            self.assertEqual(data["status"], "success")
            self.assertEqual(data["report"], "# 优化报告")
            self.assertEqual(len(data["steps"]), 1)

            # 任务成功回填 24h 报告缓存：再次提交直接命中缓存，不建新任务
            resp = self._post_submit()
            body = resp.json()
            self.assertTrue(body["data"]["hit_cache"])
            self.assertEqual(body["data"]["report"], "# 优化报告")
            self.assertEqual(AIOptimizeTask.objects.count(), 1)

        # 记账：一次真实调用 + 一次缓存命中
        logs = list(AIUsageLog.objects.filter(capability="sql_optimize").order_by("id"))
        self.assertEqual(len(logs), 2)
        self.assertFalse(logs[0].cache_hit)
        self.assertEqual(logs[0].prompt_tokens, 10)
        self.assertTrue(logs[1].cache_hit)
        self.assertEqual(logs[1].prompt_tokens, 0)

    def test_submit_dedupes_running_task(self):
        """已有 pending/running 同指纹任务时复用 task_id，不重复建任务。"""
        import threading
        from sql.models import AIOptimizeTask

        class NonRunningExecutor:
            def __init__(self):
                self.submitted = []

            def submit(self, fn):
                self.submitted.append(fn)

        mock_client = self._mock_client()
        executor = NonRunningExecutor()
        p1, p2, p3, p4, p5 = self._patch_ai(mock_client, (executor, threading.Semaphore(99)))
        self.client.force_login(self.superuser)
        with p1, p2 as mock_cfg, p3, p4, p5:
            mock_cfg.return_value.get.return_value = "model-a"
            resp1 = self._post_submit()
            task_id1 = resp1.json()["data"]["task_id"]
            self.assertEqual(AIOptimizeTask.objects.get(id=task_id1).status, "pending")
            resp2 = self._post_submit()
            body = resp2.json()
            self.assertTrue(body["data"].get("reused"))
            self.assertEqual(body["data"]["task_id"], task_id1)
            self.assertEqual(AIOptimizeTask.objects.count(), 1)

    def test_poll_forbidden_for_other_user(self):
        """非任务发起人（非超管）轮询被拒，防任务枚举。"""
        import threading
        from sql.models import AIOptimizeTask

        class NonRunningExecutor:
            def submit(self, fn):
                pass

        mock_client = self._mock_client()
        p1, p2, p3, p4, p5 = self._patch_ai(
            mock_client, (NonRunningExecutor(), threading.Semaphore(99))
        )
        self.client.force_login(self.superuser)
        with p1, p2 as mock_cfg, p3, p4, p5:
            mock_cfg.return_value.get.return_value = "model-a"
            resp = self._post_submit()
            task_id = resp.json()["data"]["task_id"]
        self.assertEqual(AIOptimizeTask.objects.get(id=task_id).status, "pending")

        self.client.force_login(self.other_user)
        resp = self.client.get(f"/api/v1/optimize/ai/async/{task_id}/")
        self.assertEqual(resp.json()["msg"], "无权查看该任务")


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
class OptimizeAIViewTest(APITestCase):
    """OptimizeAIView：缓存键含模型名 + 统一用量记账（含缓存命中）"""

    def setUp(self):
        # LocMemCache 在测试间不自动清空，显式清理避免用例间缓存串扰
        from django.core.cache import cache

        cache.clear()
        self.superuser = User.objects.create(
            username="ai_opt_user", display="测试", is_active=True, is_superuser=True
        )
        self.superuser.set_password("test_password")
        self.superuser.save()
        self.client.force_login(self.superuser)

    def _post(self):
        return self.client.post(
            "/api/v1/optimize/ai/", {"sql_content": "select * from t where id = 1"}
        )

    def test_cache_hit_records_usage(self):
        """同模型同 SQL 第二次请求命中缓存；真实调用与缓存命中均记账。"""
        mock_client_cls = MagicMock()
        mock_client_cls.return_value.optimize_sql_by_openai.return_value = "mock report"
        with patch(
            "sql_api.api_slowquery.check_openai_config", return_value=True
        ), patch(
            "sql_api.api_slowquery.SysConfig"
        ) as mock_cfg, patch(
            "sql_api.api_slowquery.OpenaiClient", return_value=mock_client_cls.return_value
        ):
            mock_cfg.return_value.get.return_value = "model-a"
            resp1 = self._post()
            self.assertEqual(resp1.json()["data"], "mock report")
            resp2 = self._post()
            self.assertEqual(resp2.json()["data"], "mock report")
            # 第二次命中缓存，AI 只被真实调用一次
            self.assertEqual(
                mock_client_cls.return_value.optimize_sql_by_openai.call_count, 1
            )
        logs = list(AIUsageLog.objects.filter(capability="sql_optimize").order_by("id"))
        self.assertEqual(len(logs), 2)
        self.assertFalse(logs[0].cache_hit)
        self.assertTrue(logs[1].cache_hit)

    def test_model_change_invalidates_cache(self):
        """切换 default_chat_model 后旧模型的缓存报告不再命中。"""
        mock_client_cls = MagicMock()
        mock_client_cls.return_value.optimize_sql_by_openai.return_value = "mock report"
        with patch(
            "sql_api.api_slowquery.check_openai_config", return_value=True
        ), patch(
            "sql_api.api_slowquery.SysConfig"
        ) as mock_cfg, patch(
            "sql_api.api_slowquery.OpenaiClient", return_value=mock_client_cls.return_value
        ):
            mock_cfg.return_value.get.return_value = "model-a"
            self._post()
            mock_cfg.return_value.get.return_value = "model-b"
            self._post()
            # 模型变了，两个模型各自真实调用一次
            self.assertEqual(
                mock_client_cls.return_value.optimize_sql_by_openai.call_count, 2
            )
        self.assertEqual(AIUsageLog.objects.filter(capability="sql_optimize").count(), 2)


class RunAgentOptimizeTest(TestCase):
    """run_agent_optimize：工具循环、client 注入（供用量记账）与预算收敛"""

    @staticmethod
    def _fake_client(responses):
        class _FakeClient:
            def __init__(self, resps):
                self.responses = list(resps)
                self.calls = []
                self.total_usage = {"prompt_tokens": 0, "completion_tokens": 0}

            def request_chat_completion(self, messages, **kwargs):
                self.calls.append(messages)
                return self.responses.pop(0)

        return _FakeClient(responses)

    @staticmethod
    def _tool_call_resp(name, arguments, call_id="call_1"):
        class _Fn:
            def __init__(self):
                self.name = name
                self.arguments = arguments

        class _ToolCall:
            def __init__(self):
                self.id = call_id
                self.function = _Fn()

        class _Msg:
            def __init__(self):
                self.content = None
                self.tool_calls = [_ToolCall()]

        class _Choice:
            def __init__(self):
                self.message = _Msg()

        class _Resp:
            def __init__(self):
                self.choices = [_Choice()]

        return _Resp()

    @staticmethod
    def _content_resp(content):
        class _Msg:
            def __init__(self):
                self.content = content
                self.tool_calls = None

        class _Choice:
            def __init__(self):
                self.message = _Msg()

        class _Resp:
            def __init__(self):
                self.choices = [_Choice()]

        return _Resp()

    def _fake_engine(self):
        queries = []

        class _RS:
            column_list = ["Table", "Create Table"]
            rows = [["t1", "CREATE TABLE t1 (id int)"]]

        class _FakeEngine:
            def query(self, db_name=None, sql="", **kwargs):
                queries.append(sql)
                return _RS()

        return _FakeEngine(), queries

    def test_tool_loop_with_injected_client(self):
        """注入 client 时循环正常工作：工具取证 → 最终报告。"""
        from sql_api.ai_optimizer import run_agent_optimize

        engine, queries = self._fake_engine()
        client = self._fake_client([
            self._tool_call_resp("get_table_ddl", '{"table": "t1"}'),
            self._content_resp("# 优化报告"),
        ])
        report, steps = run_agent_optimize(
            engine, "db1", "select * from t1 where id = 1", ["t1"], client=client
        )
        self.assertEqual(report, "# 优化报告")
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["tool"], "get_table_ddl")
        self.assertTrue(steps[0]["ok"])
        self.assertIn("SHOW CREATE TABLE `t1`", queries[0])

    def test_budget_exhaustion_forces_final_answer(self):
        """轮数预算用尽后强制模型收敛输出最终报告。"""
        from sql_api.ai_optimizer import run_agent_optimize, AGENT_MAX_ROUNDS

        engine, _ = self._fake_engine()
        responses = [
            self._tool_call_resp("get_table_indexes", '{"table": "t1"}', call_id=f"c{i}")
            for i in range(AGENT_MAX_ROUNDS)
        ]
        responses.append(self._content_resp("收敛报告"))
        client = self._fake_client(responses)
        report, steps = run_agent_optimize(engine, "db1", "select 1", [], client=client)
        self.assertEqual(report, "收敛报告")
        self.assertEqual(len(steps), AGENT_MAX_ROUNDS)
        # MAX_ROUNDS 轮工具调用 + 1 次强制收敛
        self.assertEqual(len(client.calls), AGENT_MAX_ROUNDS + 1)


class DiagnoseUsageRecordTest(TestCase):
    """诊断任务：成功后写入 AIUsageLog（统一用量流水）"""

    def test_success_records_usage(self):
        from sql.models import AIDiagnosisTask
        from sql_api.api_slowquery_v2 import diagnose_slowquery_task

        user = User.objects.create(username="diag_user", is_active=True)
        ins = Instance.objects.create(
            instance_name="diag_ins", type="master", db_type="mysql",
            host="some_host", port=3306, user="ins_user", password="some_str",
        )
        task = AIDiagnosisTask.objects.create(
            user=user, instance=ins, db_name="db1", sql_hash="hash1",
            status="pending", model="m",
        )
        with patch(
            "sql_api.api_slowquery_v2._collect_stats",
            return_value={"sample_sql": "select * from t where id = 1"},
        ), patch(
            "sql_api.api_slowquery_v2._collect_trend", return_value="趋势平稳"
        ), patch(
            "sql_api.api_slowquery_v2._collect_table_schemas", return_value="DDL"
        ), patch(
            "sql_api.api_slowquery_v2._collect_explain", return_value="EXPLAIN"
        ), patch(
            "common.utils.openai.OpenaiClient"
        ) as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.default_chat_model = "test-model"
            mock_client.diagnose_slowquery_by_openai.return_value = {
                "root_cause": "缺索引", "severity": "high",
                "bottleneck_type": "missing_index", "evidence": [],
                "suggestions": [], "confidence": 0.8,
                "_prompt_tokens": 100, "_completion_tokens": 20,
            }
            diagnose_slowquery_task(task.id)
        task.refresh_from_db()
        self.assertEqual(task.status, "success")
        self.assertEqual(task.prompt_tokens, 100)
        log = AIUsageLog.objects.get(capability="slowquery_diagnosis")
        self.assertEqual(log.instance_name, "diag_ins")
        self.assertEqual(log.user_name, "diag_user")
        self.assertEqual(log.db_name, "db1")
        self.assertEqual(log.status, "success")
