# -*- coding: UTF-8 -*-
"""
@author: hhyo
@license: Apache Licence
@file: tests.py
@time: 2019/03/04
"""

import json
import os
import tempfile
from django.test import Client, TestCase
from unittest.mock import patch, ANY, Mock
from pytest_mock import MockerFixture
from django.contrib.auth import get_user_model

from sql.plugins.my2sql import My2SQL, describe_failure, is_failure
from sql.plugins.schemasync import SchemaSync
from sql.plugins.soar import Soar
from sql.plugins.sqladvisor import SQLAdvisor
from sql.plugins.pt_archiver import PtArchiver
from sql.plugins.password import VaultMixin

from common.config import SysConfig

User = get_user_model()

__author__ = "hhyo"


class TestPlugin(TestCase):
    """
    测试Plugin调用
    """

    @classmethod
    def setUpClass(cls):
        cls.superuser = User(username="super", is_superuser=True)
        cls.superuser.save()
        cls.sys_config = SysConfig()
        cls.client = Client()
        cls.client.force_login(cls.superuser)

    @classmethod
    def tearDownClass(cls):
        cls.superuser.delete()
        cls.sys_config.replace(json.dumps({}))

    def test_check_args_path(self):
        """
        测试路径
        :return:
        """
        args = {
            "online-dsn": "",
            "test-dsn": "",
            "allow-online-as-test": "false",
            "report-type": "markdown",
            "query": "select 1;",
        }
        self.sys_config.set("soar", "")
        self.sys_config.get_all_config()
        soar = Soar()
        args_check_result = soar.check_args(args)
        self.assertDictEqual(
            args_check_result,
            {"status": 1, "msg": "可执行文件路径不能为空！", "data": {}},
        )
        # 路径不为空
        self.sys_config.set("soar", "/opt/dbshield/src/plugins/soar")
        self.sys_config.get_all_config()
        soar = Soar()
        args_check_result = soar.check_args(args)
        self.assertDictEqual(args_check_result, {"status": 0, "msg": "ok", "data": {}})

    def test_check_args_disable(self):
        """
        测试禁用参数
        :return:
        """
        args = {
            "online-dsn": "",
            "test-dsn": "",
            "allow-online-as-test": "false",
            "report-type": "markdown",
            "query": "select 1;",
        }
        self.sys_config.set("soar", "/opt/dbshield/src/plugins/soar")
        self.sys_config.get_all_config()
        soar = Soar()
        soar.disable_args = ["allow-online-as-test"]
        args_check_result = soar.check_args(args)
        self.assertDictEqual(
            args_check_result,
            {"status": 1, "msg": "allow-online-as-test参数已被禁用", "data": {}},
        )

    def test_check_args_required(self):
        """
        测试必选参数
        :return:
        """
        args = {
            "online-dsn": "",
            "test-dsn": "",
            "allow-online-as-test": "false",
            "report-type": "markdown",
        }
        self.sys_config.set("soar", "/opt/dbshield/src/plugins/soar")
        self.sys_config.get_all_config()
        soar = Soar()
        soar.required_args = ["query"]
        args_check_result = soar.check_args(args)
        self.assertDictEqual(
            args_check_result, {"status": 1, "msg": "必须指定query参数", "data": {}}
        )
        args["query"] = ""
        args_check_result = soar.check_args(args)
        self.assertDictEqual(
            args_check_result, {"status": 1, "msg": "query参数值不能为空", "data": {}}
        )

    def test_soar_generate_args2cmd(self):
        """
        测试SOAR参数转换
        :return:
        """
        args = {
            "online-dsn": "",
            "test-dsn": "",
            "allow-online-as-test": "false",
            "report-type": "markdown",
            "query": "select 1;",
        }
        self.sys_config.set("soar", "/opt/dbshield/src/plugins/soar")
        self.sys_config.get_all_config()
        soar = Soar()
        cmd_args = soar.generate_args2cmd(args)
        self.assertIsInstance(cmd_args, list)

    def test_sql_advisor_generate_args2cmd(self):
        """
        测试sql_advisor参数转换
        :return:
        """
        args = {
            "h": "mysql",
            "P": 3306,
            "u": "root",
            "p": "",
            "d": "archery",
            "v": 1,
            "q": "select 1;",
        }
        self.sys_config.set("sqladvisor", "/opt/dbshield/src/plugins/SQLAdvisor")
        self.sys_config.get_all_config()
        sql_advisor = SQLAdvisor()
        cmd_args = sql_advisor.generate_args2cmd(args)
        self.assertIsInstance(cmd_args, list)

    def test_schema_sync_generate_args2cmd(self):
        """
        测试schema_sync参数转换
        :return:
        """
        args = {
            "sync-auto-inc": True,
            "sync-comments": True,
            "tag": "tag_v",
            "output-directory": "",
            "source": r"mysql://{user}:{pwd}@{host}:{port}/{database}".format(
                user="root", pwd="123456", host="127.0.0.1", port=3306, database="*"
            ),
            "target": r"mysql://{user}:{pwd}@{host}:{port}/{database}".format(
                user="root", pwd="123456", host="127.0.0.1", port=3306, database="*"
            ),
        }
        self.sys_config.set("schemasync", "/opt/venv4schemasync/bin/schemasync")
        self.sys_config.get_all_config()
        schema_sync = SchemaSync()
        cmd_args = schema_sync.generate_args2cmd(args)
        self.assertIsInstance(cmd_args, list)

    def test_my2sql_build_args_flag_names(self):
        """
        测试my2sql参数名与工具 flag 一致（写错名字会导致整次解析 exit 2 失败）
        :return:
        """
        self.sys_config.set("my2sql", "/opt/dbshield/src/plugins/my2sql")
        self.sys_config.get_all_config()
        my2sql = My2SQL()
        args = My2SQL.build_args(
            host="127.0.0.1",
            port=3306,
            user="root",
            password="123456",
            output_dir="/tmp/my2sql",
            work_type="rollback",
            threads=2,
            start_file="mysql-bin.000043",
            start_pos=4,
            stop_file="mysql-bin.000043",
            stop_pos=1000,
            databases="db1,db2",
            tables="tb1",
            sql_types=["insert", "update"],
            add_extra_info=True,
            ignore_primary_key=True,
            no_db_prefix=True,
        )
        cmd_args = my2sql.generate_args2cmd(args)
        self.assertIsInstance(cmd_args, list)
        # 列表参数必须是逗号分隔字符串，不能是 python 列表字面量
        self.assertEqual(cmd_args[cmd_args.index("-databases") + 1], "db1,db2")
        self.assertEqual(cmd_args[cmd_args.index("-tables") + 1], "tb1")
        self.assertEqual(cmd_args[cmd_args.index("-sql") + 1], "insert,update")
        # flag 名必须与 my2sql 一致
        self.assertIn("-ignore-primaryKey-forInsert", cmd_args)
        self.assertIn("-do-not-add-prifixDb", cmd_args)
        self.assertIn("-add-extraInfo", cmd_args)
        self.assertIn("-mode", cmd_args)
        self.assertNotIn("-ignore-primary-key-for-rollback", cmd_args)
        self.assertNotIn("-no-db-prefix", cmd_args)
        # 未启用的开关不应出现在命令行里
        self.assertNotIn("-full-columns", cmd_args)
        self.assertNotIn("-file-per-table", cmd_args)

    def test_my2sql_failure_from_stdout(self):
        """
        测试失败判定：my2sql 日志在 stdout，解析中断（[fatal]）不能被当成成功
        :return:
        """
        fatal = (
            "[2026/09/23 09:17:00] [fatal] events.go:87 db.tb column count 5 in binlog > "
            "in table structure 4, usually means DDL in the middle"
        )
        self.assertTrue(is_failure(fatal, "", 1))
        self.assertTrue(is_failure("", "flag provided but not defined: -x", 2))
        self.assertFalse(is_failure("[info] finish", "", 0))
        self.assertIn("DDL", describe_failure(fatal, "", 1))

    def test_my2sql_collect_sql_rows(self):
        """
        测试读取 my2sql 输出：跳过隐藏文件（rollback 中间文件），保留 # 附加信息
        :return:
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            with open(os.path.join(tmp_dir, "forward.1.sql"), "w", encoding="utf-8") as f:
                f.write(
                    "# datetime=2020-07-16_10:44:09 database=d1 table=t1\n"
                    "INSERT INTO `d1`.`t1` (`id`) VALUES (1);\n"
                    "COMMIT;\n"
                )
            # rollback 模式的中间文件是 .<db>.<tb>.rollback.<n>.sql，应被跳过
            with open(os.path.join(tmp_dir, ".d1.t1.rollback.1.sql"), "w", encoding="utf-8") as f:
                f.write("DELETE FROM `d1`.`t1` WHERE `id`=1;\n")
            rows = My2SQL.collect_sql_rows(tmp_dir, limit=10)
            self.assertEqual(1, len(rows))
            self.assertIn("INSERT INTO", rows[0]["sql"])
            self.assertIn("database=d1", rows[0]["extra_info"])
            self.assertEqual(1, My2SQL.count_sql_rows(tmp_dir))

    def test_my2sql_generate_args2cmd(self):
        """
        测试my2sql参数转换
        :return:
        """
        args = {
            "conn_options": "-host mysql -user root -password '123456' -port 3306 ",
            "work-type": "2sql",
            "start-file": "mysql-bin.000043",
            "start-pos": 111,
            "stop-file": "",
            "stop-pos": "",
            "start-datetime": "",
            "stop-datetime": "",
            "databases": "account_center",
            "tables": "ac_apps",
            "sql": "update",
            "threads": 1,
            "add-extraInfo": "false",
            "ignore-primaryKey-forInsert": "false",
            "full-columns": "false",
            "do-not-add-prifixDb": "false",
            "file-per-table": "false",
        }
        self.sys_config.set("my2sql", "/opt/dbshield/src/plugins/my2sql")
        self.sys_config.get_all_config()
        my2sql = My2SQL()
        cmd_args = my2sql.generate_args2cmd(args)
        self.assertIsInstance(cmd_args, list)

    def test_pt_archiver_generate_args2cmd(self):
        """
        测试pt_archiver参数转换
        :return:
        """
        args = {
            "no-version-check": True,
            "source": "",
            "where": "",
            "progress": 5000,
            "statistics": True,
            "charset": "UTF8",
            "limit": 10000,
            "txn-size": 1000,
            "sleep": 1,
        }
        pt_archiver = PtArchiver()
        cmd_args = pt_archiver.generate_args2cmd(args)
        self.assertIsInstance(cmd_args, list)

    @patch("sql.plugins.plugin.subprocess")
    def test_execute_cmd(self, mock_subprocess):
        args = {
            "online-dsn": "",
            "test-dsn": "",
            "allow-online-as-test": "false",
            "report-type": "markdown",
            "query": "select 1;",
        }
        self.sys_config.set("soar", "/opt/dbshield/src/plugins/soar")
        self.sys_config.get_all_config()
        soar = Soar()
        cmd_args = soar.generate_args2cmd(args)

        mock_subprocess.Popen.return_value.communicate.return_value = (
            "some_stdout",
            "some_stderr",
        )
        stdout, stderr = soar.execute_cmd(cmd_args).communicate()
        mock_subprocess.Popen.assert_called_once_with(
            cmd_args, shell=False, stdout=ANY, stderr=ANY, universal_newlines=ANY
        )
        self.assertIn("some_stdout", stdout)
        # 异常

        mock_subprocess.Popen.side_effect = Exception("Boom! some exception!")
        with self.assertRaises(RuntimeError):
            soar.execute_cmd(cmd_args)


class TestSoar(TestCase):
    """
    测试Soar的拓展方法
    """

    @classmethod
    def setUpClass(cls):
        soar_path = "/opt/dbshield/src/plugins/soar"  # 修改为本机的soar路径
        cls.superuser = User(username="super", is_superuser=True)
        cls.superuser.save()
        cls.client = Client()
        cls.client.force_login(cls.superuser)
        cls.sys_config = SysConfig()
        cls.sys_config.set("soar", soar_path)
        cls.sys_config.get_all_config()
        cls.soar = Soar()

    @classmethod
    def tearDownClass(cls):
        cls.superuser.delete()
        cls.sys_config.replace(json.dumps({}))

    @patch("sql.plugins.plugin.subprocess")
    def test_fingerprint(self, _subprocess):
        """
        测试SQL指纹打印，未断言
        :return:
        """
        sql = """select * from sql_users where id>0 and email<>'';"""
        self.soar.fingerprint(sql)

    @patch("sql.plugins.plugin.subprocess")
    def test_compress(self, _subprocess):
        """
        测试SQL压缩，未断言
        :return:
        """
        sql = """
        select * 
        from sql_users
        where id>0 and email<>'';
        """
        self.soar.compress(sql)

    @patch("sql.plugins.plugin.subprocess")
    def test_pretty(self, _subprocess):
        """
        测试SQL美化，未断言
        :return:
        """
        sql = """select * from sql_users where id>0 and email<>'';"""
        self.soar.pretty(sql)

    @patch("sql.plugins.plugin.subprocess")
    def test_remove_comment(self, _subprocess):
        """
        测试去除注释，未断言
        :return:
        """
        sql = """--
                select *
                from sql_users
                where id = 1 -- and email<>''
                -- and username<>''
                # and ;"""
        self.soar.remove_comment(sql)

    @patch("sql.plugins.plugin.subprocess")
    def test_rewrite(self, _subprocess):
        """
        测试SQL改写
        :return:
        """
        sql = """update sql_users set username='',id=1 where id>0 and email<>'';"""
        self.soar.rewrite(sql)
        # 异常测试
        with self.assertRaises(RuntimeError):
            self.soar.rewrite(sql, "unknown")


def test_password_mixin(mocker: MockerFixture):
    from sql.plugins.password import requests

    class MockReponse(Mock):
        def json(self):
            return {"data": {"username": "test", "password": "test", "ttl": 360}}

    mocker.patch.object(requests, "get", return_value=MockReponse())

    class DummyInstance:
        instance_name = "dummy"

    class CompondInstance(DummyInstance, VaultMixin):
        pass

    instance = CompondInstance()
    username, password = instance.get_username_password()
    assert username == "test"
    assert password == "test"
    assert requests.get.call_count == 1
