"""engine base库, 包含一个``EngineBase`` class和一个get_engine函数"""

import importlib
import re
from sql.engines.models import ResultSet, ReviewSet
from sql.models import Instance
from sql.utils.ssh_tunnel import SSHConnection
from django.conf import settings


class EngineBase:
    """enginebase 只定义了init函数和若干方法的名字, 具体实现用mysql.py pg.py等实现"""

    test_query = None

    name = "Base"
    info = "base engine"

    def __init__(self, instance: Instance = None):
        self.conn = None
        self.thread_id = None
        if instance:
            self.instance = instance  # type: Instance
            self.instance_name = instance.instance_name
            self.host = instance.host
            self.port = int(instance.port)
            self.user, self.password = self.instance.get_username_password()
            self.db_name = instance.db_name
            self.mode = instance.mode

            # 判断如果配置了隧道则连接隧道，只测试了MySQL
            if self.instance.tunnel:
                self.ssh = SSHConnection(
                    self.host,
                    self.port,
                    instance.tunnel.host,
                    instance.tunnel.port,
                    instance.tunnel.user,
                    instance.tunnel.password,
                    instance.tunnel.pkey,
                    instance.tunnel.pkey_password,
                )
                self.host, self.port = self.ssh.get_ssh()

    def __del__(self):
        if hasattr(self, "ssh"):
            del self.ssh
        if hasattr(self, "remotessh"):
            del self.remotessh

    def remote_instance_conn(self, instance=None):
        user, password = instance.get_username_password()
        # 判断如果配置了隧道则连接隧道
        if not hasattr(self, "remotessh") and instance.tunnel:
            self.remotessh = SSHConnection(
                instance.host,
                instance.port,
                instance.tunnel.host,
                instance.tunnel.port,
                instance.tunnel.user,
                instance.tunnel.password,
                instance.tunnel.pkey,
                instance.tunnel.pkey_password,
            )
            self.remote_host, self.remote_port = self.remotessh.get_ssh()
            user, password = instance.get_username_password()
            self.remote_user = user
            self.remote_password = password
        elif not instance.tunnel:
            self.remote_host = instance.host
            self.remote_port = instance.port
            self.remote_user = user
            self.remote_password = password
        return (
            self.remote_host,
            self.remote_port,
            self.remote_user,
            self.remote_password,
        )

    def get_connection(self, db_name=None):
        """返回一个conn实例"""

    def test_connection(self):
        """测试实例链接是否正常"""
        return self.query(sql=self.test_query)

    def escape_string(self, value: str) -> str:
        """参数转义"""
        return value

    @property
    def auto_backup(self):
        """是否支持备份"""
        return False

    @property
    def seconds_behind_master(self):
        """实例同步延迟情况"""
        return None

    @property
    def server_version(self):
        """返回引擎服务器版本，返回对象为tuple (x,y,z)"""
        return tuple()

    def processlist(self, command_type, **kwargs) -> ResultSet:
        """获取连接信息"""
        return ResultSet()

    def kill_connection(self, thread_id):
        """终止数据库连接"""

    def close(self):
        """关闭并清空缓存连接（各引擎 get_connection 自行维护 self.conn）"""
        if self.conn:
            self.conn.close()
            self.conn = None

    @staticmethod
    def get_backup_connection():
        """goInception 备份库连接（goinception/oracle 等引擎共用）"""
        import MySQLdb

        from common.config import SysConfig

        archer_config = SysConfig()
        backup_host = archer_config.get("inception_remote_backup_host")
        backup_port = int(archer_config.get("inception_remote_backup_port", 3306))
        backup_user = archer_config.get("inception_remote_backup_user")
        backup_password = archer_config.get("inception_remote_backup_password", "")
        return MySQLdb.connect(
            host=backup_host,
            port=backup_port,
            user=backup_user,
            passwd=backup_password,
            charset="utf8mb4",
            autocommit=True,
        )

    def rewrite_limit_sql(self, sql="", limit_num=0):
        """对查询 SQL 增加 limit 限制：limit n / limit n offset m / limit m,n
        统一改写为 limit n（mysql/clickhouse/tdengine 的 filter_sql 共用实现）。
        """
        sql = sql.rstrip(";").strip()
        if re.match(r"^select", sql, re.I):
            # LIMIT N
            limit_n = re.compile(r"limit\s+(\d+)\s*$", re.I)
            # LIMIT M OFFSET N
            limit_offset = re.compile(r"limit\s+(\d+)\s+offset\s+(\d+)\s*$", re.I)
            # LIMIT M,N
            offset_comma_limit = re.compile(r"limit\s+(\d+)\s*,\s*(\d+)\s*$", re.I)
            if limit_n.search(sql):
                sql_limit = limit_n.search(sql).group(1)
                limit_num = min(int(limit_num), int(sql_limit))
                sql = limit_n.sub(f"limit {limit_num};", sql)
            elif limit_offset.search(sql):
                sql_limit = limit_offset.search(sql).group(1)
                sql_offset = limit_offset.search(sql).group(2)
                limit_num = min(int(limit_num), int(sql_limit))
                sql = limit_offset.sub(f"limit {limit_num} offset {sql_offset};", sql)
            elif offset_comma_limit.search(sql):
                sql_offset = offset_comma_limit.search(sql).group(1)
                sql_limit = offset_comma_limit.search(sql).group(2)
                limit_num = min(int(limit_num), int(sql_limit))
                sql = offset_comma_limit.sub(f"limit {sql_offset},{limit_num};", sql)
            else:
                sql = f"{sql} limit {limit_num};"
        else:
            sql = f"{sql};"
        return sql

    def execute_workflow(self, workflow):
        """执行上线单（默认委托 execute；有备份/只读等前置逻辑的引擎自行覆写）"""
        return self.execute(
            db_name=workflow.db_name, sql=workflow.sqlworkflowcontent.sql_content
        )

    def get_all_databases(self):
        """获取数据库列表, 返回一个ResultSet，rows=list"""
        return ResultSet()

    def get_all_tables(self, db_name, **kwargs):
        """获取table 列表, 返回一个ResultSet，rows=list"""
        return ResultSet()

    def get_group_tables_by_db(self, db_name, **kwargs):
        """获取首字符分组的table列表，返回一个dict"""
        return dict()

    def get_table_meta_data(self, db_name, tb_name, **kwargs):
        """获取表格元信息"""
        return dict()

    def get_table_desc_data(self, db_name, tb_name, **kwargs):
        """获取表格字段信息"""
        return dict()

    def get_table_index_data(self, db_name, tb_name, **kwargs):
        """获取表格索引信息"""
        return dict()

    def get_tables_metas_data(self, db_name, **kwargs):
        """获取数据库所有表格信息，用作数据字典导出接口"""
        return list()

    def get_views_list(self, db_name, **kwargs):
        """获取视图列表, 返回 dict"""
        return dict()

    def get_view_detail(self, db_name, view_name, **kwargs):
        """获取视图详情, 返回 dict"""
        return dict()

    def get_triggers_list(self, db_name, **kwargs):
        """获取触发器列表, 返回 dict"""
        return dict()

    def get_trigger_detail(self, db_name, trigger_name, **kwargs):
        """获取触发器详情, 返回 dict"""
        return dict()

    def get_procedures_list(self, db_name, **kwargs):
        """获取存储过程列表, 返回 dict"""
        return dict()

    def get_procedure_detail(self, db_name, proc_name, **kwargs):
        """获取存储过程详情, 返回 dict"""
        return dict()

    def get_functions_list(self, db_name, **kwargs):
        """获取函数列表, 返回 dict"""
        return dict()

    def get_function_detail(self, db_name, func_name, **kwargs):
        """获取函数详情, 返回 dict"""
        return dict()

    def get_events_list(self, db_name, **kwargs):
        """获取定时任务列表, 返回 dict"""
        return dict()

    def get_event_detail(self, db_name, event_name, **kwargs):
        """获取定时任务详情, 返回 dict"""
        return dict()

    def get_all_databases_summary(self):
        """实例数据库管理功能，获取实例所有的数据库描述信息"""
        return ResultSet()

    def get_instance_users_summary(self):
        """实例账号管理功能，获取实例所有账号信息"""
        return ResultSet()

    def create_instance_user(self, **kwargs):
        """实例账号管理功能，创建实例账号"""
        return ResultSet()

    def drop_instance_user(self, **kwargs):
        """实例账号管理功能，删除实例账号"""
        return ResultSet()

    def reset_instance_user_pwd(self, **kwargs):
        """实例账号管理功能，重置实例账号密码"""
        return ResultSet()

    def get_all_columns_by_tb(self, db_name, tb_name, **kwargs):
        """获取所有字段, 返回一个ResultSet，rows=list"""
        return ResultSet()

    def describe_table(self, db_name, tb_name, **kwargs):
        """获取表结构, 返回一个 ResultSet，rows=list"""
        return ResultSet()

    def query_check(self, db_name=None, sql=""):
        """查询语句的检查、注释去除、切分, 返回一个字典 {'bad_query': bool, 'filtered_sql': str}"""

    def filter_sql(self, sql="", limit_num=0):
        """给查询语句增加结果级限制或者改写语句, 返回修改后的语句"""
        return sql.strip()

    def query(
        self,
        db_name=None,
        sql="",
        limit_num=0,
        close_conn=True,
        parameters=None,
        **kwargs,
    ):
        """实际查询 返回一个ResultSet"""
        return ResultSet()

    def query_masking(self, db_name=None, sql="", resultset=None):
        """传入 sql语句, db名, 结果集,
        返回一个脱敏后的结果集"""
        return resultset

    def execute_check(self, db_name=None, sql=""):
        """执行语句的检查 返回一个ReviewSet"""
        return ReviewSet()

    def execute(self, **kwargs):
        """执行语句 返回一个ReviewSet"""
        return ReviewSet()

    def get_execute_percentage(self):
        """获取执行进度"""

    def get_rollback(self, workflow):
        """获取工单回滚语句"""
        return list()

    def get_variables(self, variables=None):
        """获取实例参数，返回一个 ResultSet"""
        return ResultSet()

    def set_variable(self, variable_name, variable_value):
        """修改实例参数值，返回一个 ResultSet"""
        return ResultSet()

    def tablespace(self, offset=0, row_count=14, schema_search=""):
        """获取表空间信息，返回一个 ResultSet"""
        return ResultSet()

    def tablespace_count(self, schema_search=""):
        """获取表空间数量，返回一个 ResultSet"""
        return ResultSet()


def get_engine_map():
    available_engines = settings.AVAILABLE_ENGINES
    enabled_engines = {}
    for e in settings.ENABLED_ENGINES:
        config = available_engines.get(e)
        if not config:
            raise ValueError(f"invalid engine {e}, not found in engine map")
        module, o = config["path"].split(":")
        engine = getattr(importlib.import_module(module), o)
        enabled_engines[e] = engine
    return enabled_engines


engine_map = get_engine_map()


def get_engine(instance=None):  # pragma: no cover
    """获取数据库操作engine"""
    if instance.db_type == "mysql":
        from sql.models import AliyunRdsConfig

        if AliyunRdsConfig.objects.filter(instance=instance, is_enable=True).exists():
            from .cloud.aliyun_rds import AliyunRDS

            return AliyunRDS(instance=instance)
    engine = engine_map.get(instance.db_type)
    if not engine:
        raise ValueError(
            f"engine {instance.db_type} not enabled or not supported, please contact admin"
        )
    return engine(instance=instance)
