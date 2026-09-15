import json
from datetime import timedelta, datetime
from unittest.mock import patch, MagicMock, ANY
from django.contrib.auth.models import Permission
from django.test import Client, TestCase, TransactionTestCase

from common.config import SysConfig
# sql.binlog / sql.query / sql.sql_workflow / sql.slowlog / sql.data_dictionary
# 等模块已随 DRF 化重构移除，打旧路由（/sqlworkflow/、/binlog/、/slowquery/ 等）
# 的死用例已一并删除，勿再引用（否则整个 tests.py 无法收集）
from sql.engines.models import ResultSet
from sql.utils.execute_sql import execute_callback
from sql.models import (
    Users,
    Instance,
    QueryPrivileges,
    SqlWorkflow,
    SqlWorkflowContent,
    QueryLog,
)

User = Users


class TestUser(TestCase):
    def setUp(self):
        self.u1 = User(username="test_user", display="中文显示", is_active=True)
        self.u1.set_password("test_password")
        self.u1.save()

    def tearDown(self):
        self.u1.delete()

    def test_out_ranged_failed_login_count(self):
        # 正常保存
        self.u1.failed_login_count = 64
        self.u1.save()
        self.u1.refresh_from_db()
        self.assertEqual(64, self.u1.failed_login_count)
        # 超过127视为127
        self.u1.failed_login_count = 256
        self.u1.save()
        self.u1.refresh_from_db()
        self.assertEqual(127, self.u1.failed_login_count)
        # 小于0视为0
        self.u1.failed_login_count = -1
        self.u1.save()
        self.u1.refresh_from_db()
        self.assertEqual(0, self.u1.failed_login_count)


class TestQuery(TransactionTestCase):
    def setUp(self):
        self.slave1 = Instance(
            instance_name="test_slave_instance",
            type="slave",
            db_type="mysql",
            host="testhost",
            port=3306,
            user="mysql_user",
            password="mysql_password",
        )
        self.slave2 = Instance(
            instance_name="test_instance_non_mysql",
            type="slave",
            db_type="mssql",
            host="some_host2",
            port=3306,
            user="some_user",
            password="some_str",
        )
        self.slave1.save()
        self.slave2.save()
        self.superuser1 = User.objects.create(username="super1", is_superuser=True)
        self.u1 = User.objects.create(
            username="test_user", display="中文显示", is_active=True
        )
        self.u2 = User.objects.create(
            username="test_user2", display="中文显示", is_active=True
        )
        self.query_log = QueryLog.objects.create(
            instance_name=self.slave1.instance_name,
            db_name="some_db",
            sqllog="select 1;",
            effect_row=10,
            cost_time=1,
            username=self.superuser1.username,
        )
        sql_query_perm = Permission.objects.get(codename="query_submit")
        self.u2.user_permissions.add(sql_query_perm)

    def tearDown(self):
        QueryPrivileges.objects.all().delete()
        QueryLog.objects.all().delete()
        self.u1.delete()
        self.u2.delete()
        self.superuser1.delete()
        self.slave1.delete()
        self.slave2.delete()
        archer_config = SysConfig()
        archer_config.set("disable_star", False)

    @patch("sql.services.sqlquery_service.user_instances")
    @patch("sql.services.sqlquery_service.get_engine")
    @patch("sql.services.sqlquery_service.query_priv_check")
    def testCorrectSQL(self, _priv_check, _get_engine, _user_instances):
        c = Client()
        some_sql = "select some from some_table limit 100;"
        some_db = "some_db"
        some_limit = 100
        c.force_login(self.u1)
        r = c.post(
            "/api/v1/sqlquery/execute/",
            data={
                "instance_name": self.slave1.instance_name,
                "sql_content": some_sql,
                "db_name": some_db,
                "limit_num": some_limit,
            },
        )
        # 无 query_submit 权限：视图层硬拦截，权限类错误统一返回 HTTP 403
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["status"], 1)
        self.assertEqual(r.json()["msg"], "无执行查询权限")
        c.force_login(self.u2)
        q_result = ResultSet(full_sql=some_sql, rows=["value"])
        q_result.column_list = ["some"]
        _get_engine.return_value.query_check.return_value = {
            "msg": "",
            "bad_query": False,
            "filtered_sql": some_sql,
            "has_star": False,
        }
        _get_engine.return_value.filter_sql.return_value = some_sql
        _get_engine.return_value.query.return_value = q_result
        _get_engine.return_value.thread_id = None
        _get_engine.return_value.seconds_behind_master = 100
        _priv_check.return_value = {
            "status": 0,
            "data": {"limit_num": 100, "priv_check": True},
        }
        _user_instances.return_value.get.return_value = self.slave1
        r = c.post(
            "/api/v1/sqlquery/execute/",
            data={
                "instance_name": self.slave1.instance_name,
                "sql_content": some_sql,
                "db_name": some_db,
                "limit_num": some_limit,
            },
        )
        _get_engine.return_value.query.assert_called_once_with(
            some_db,
            some_sql,
            some_limit,
            schema_name=None,
            tb_name=None,
            max_execution_time=60000,
        )
        r_json = r.json()
        self.assertEqual(r_json["data"]["rows"], ["value"])
        self.assertEqual(r_json["data"]["column_list"], ["some"])
        self.assertEqual(r_json["data"]["seconds_behind_master"], 100)

    @patch("sql.services.sqlquery_service.user_instances")
    @patch("sql.services.sqlquery_service.get_engine")
    @patch("sql.services.sqlquery_service.query_priv_check")
    def testSQLWithoutLimit(self, _priv_check, _get_engine, _user_instances):
        c = Client()
        some_limit = 100
        sql_without_limit = "select some from some_table"
        sql_with_limit = "select some from some_table limit {0};".format(some_limit)
        some_db = "some_db"
        c.force_login(self.u2)
        q_result = ResultSet(full_sql=sql_without_limit, rows=["value"])
        q_result.column_list = ["some"]
        _get_engine.return_value.query_check.return_value = {
            "msg": "",
            "bad_query": False,
            "filtered_sql": sql_without_limit,
            "has_star": False,
        }
        _get_engine.return_value.filter_sql.return_value = sql_with_limit
        _get_engine.return_value.query.return_value = q_result
        _get_engine.return_value.thread_id = None
        _get_engine.return_value.seconds_behind_master = 0
        _priv_check.return_value = {
            "status": 0,
            "data": {"limit_num": 100, "priv_check": True},
        }
        _user_instances.return_value.get.return_value = self.slave1
        r = c.post(
            "/api/v1/sqlquery/execute/",
            data={
                "instance_name": self.slave1.instance_name,
                "sql_content": sql_without_limit,
                "db_name": some_db,
                "limit_num": some_limit,
            },
        )
        _get_engine.return_value.query.assert_called_once_with(
            some_db,
            sql_with_limit,
            some_limit,
            schema_name=None,
            tb_name=None,
            max_execution_time=60000,
        )
        r_json = r.json()
        self.assertEqual(r_json["data"]["rows"], ["value"])
        self.assertEqual(r_json["data"]["column_list"], ["some"])

    @patch("sql.services.sqlquery_service.query_priv_check")
    def testStarOptionOn(self, _priv_check):
        c = Client()
        c.force_login(self.u2)
        some_limit = 100
        sql_with_star = "select * from some_table"
        some_db = "some_db"
        _priv_check.return_value = {
            "status": 0,
            "data": {"limit_num": 100, "priv_check": True},
        }
        archer_config = SysConfig()
        archer_config.set("disable_star", True)
        r = c.post(
            "/api/v1/sqlquery/execute/",
            data={
                "instance_name": self.slave1.instance_name,
                "sql_content": sql_with_star,
                "db_name": some_db,
                "limit_num": some_limit,
            },
        )
        archer_config.set("disable_star", False)
        r_json = r.json()
        self.assertEqual(1, r_json["status"])

    # test_kill_query_conn：被测函数 kill_query_conn 随 sql/query.py 删除而移除

    def test_query_log(self):
        """测试获取查询历史"""
        c = Client()
        c.force_login(self.superuser1)
        QueryLog(id=self.query_log.id, favorite=True, alias="test_a").save(
            update_fields=["favorite", "alias"]
        )
        data = {
            "star": "true",
            "query_log_id": self.query_log.id,
            "limit": 14,
            "offset": 0,
        }
        r = c.get("/api/v1/sqlquery/logs/", data=data)
        self.assertEqual(r.json()["total"], 1)

    def test_star(self):
        """测试查询语句收藏"""
        c = Client()
        c.force_login(self.superuser1)
        r = c.post(
            "/api/v1/sqlquery/favorites/",
            data={
                "query_log_id": self.query_log.id,
                "star": "true",
                "alias": "test_alias",
            },
        )
        query_log = QueryLog.objects.get(id=self.query_log.id)
        self.assertTrue(query_log.favorite)
        self.assertEqual(query_log.alias, "test_alias")

    def test_un_star(self):
        """测试查询语句取消收藏"""
        c = Client()
        c.force_login(self.superuser1)
        r = c.post(
            "/api/v1/sqlquery/favorites/",
            data={"query_log_id": self.query_log.id, "star": "false", "alias": ""},
        )
        r_json = r.json()
        query_log = QueryLog.objects.get(id=self.query_log.id)
        self.assertFalse(query_log.favorite)
        self.assertEqual(query_log.alias, "")


class TestAsync(TestCase):
    def setUp(self):
        self.now = datetime.now()
        self.u1 = User(username="some_user", display="用户1")
        self.u1.save()
        self.master1 = Instance(
            instance_name="test_master_instance",
            type="master",
            db_type="mysql",
            host="testhost",
            port=3306,
            user="mysql_user",
            password="mysql_password",
        )
        self.master1.save()
        self.wf1 = SqlWorkflow.objects.create(
            workflow_name="some_name2",
            group_id=1,
            group_name="g1",
            engineer=self.u1.username,
            engineer_display=self.u1.display,
            audit_auth_groups="some_group",
            create_time=self.now - timedelta(days=1),
            status="workflow_executing",
            is_backup=True,
            instance=self.master1,
            db_name="some_db",
            syntax_type=1,
        )
        self.wfc1 = SqlWorkflowContent.objects.create(
            workflow=self.wf1, sql_content="some_sql", execute_result=""
        )
        # 初始化工单执行返回对象
        self.task_result = MagicMock()
        self.task_result.args = [self.wf1.id]
        self.task_result.success = True
        self.task_result.stopped = self.now
        self.task_result.result.json.return_value = json.dumps(
            [{"id": 1, "sql": "some_content"}]
        )
        self.task_result.result.warning = ""
        self.task_result.result.error = ""

    def tearDown(self):
        self.wf1.delete()
        self.u1.delete()
        self.task_result = None
        self.master1.delete()

    @patch("sql.utils.execute_sql.notify_for_execute")
    @patch("sql.utils.execute_sql.Audit")
    def test_call_back(self, mock_audit, mock_notify):
        mock_audit.detail_by_workflow_id.return_value.audit_id = 123
        mock_audit.add_log.return_value = "any thing"
        execute_callback(self.task_result)
        mock_audit.detail_by_workflow_id.assert_called_with(
            workflow_id=self.wf1.id, workflow_type=ANY
        )
        mock_audit.add_log.assert_called_with(
            audit_id=123,
            operation_type=ANY,
            operation_type_desc=ANY,
            operation_info="执行结果：已正常结束",
            operator=ANY,
            operator_display=ANY,
        )
        mock_notify.assert_called_once()
