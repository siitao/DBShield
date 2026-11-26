# -*- coding: UTF-8 -*-
import logging
import re
import traceback
import MySQLdb
from pymysql.converters import escape_string
import simplejson as json

from common.config import SysConfig
from sql.models import AliyunRdsConfig
from sql.utils.sql_utils import get_syntax_type, quote_db_ident
from . import EngineBase
from .models import ResultSet, ReviewSet, ReviewResult

logger = logging.getLogger("default")


class GoInceptionEngine(EngineBase):
    test_query = "INCEPTION GET VARIABLES"

    name = "GoInception"

    info = "GoInception engine"

    def get_connection(self, db_name=None):
        if self.conn:
            return self.conn
        if hasattr(self, "instance"):
            self.conn = MySQLdb.connect(
                host=self.host,
                port=self.port,
                charset=self.instance.charset or "utf8mb4",
                connect_timeout=10,
            )
            return self.conn
        archer_config = SysConfig()
        go_inception_host = archer_config.get("go_inception_host")
        go_inception_port = int(archer_config.get("go_inception_port", 4000))
        go_inception_user = archer_config.get("go_inception_user", "")
        go_inception_password = archer_config.get("go_inception_password", "")
        self.conn = MySQLdb.connect(
            host=go_inception_host,
            port=go_inception_port,
            user=go_inception_user,
            passwd=go_inception_password,
            charset="utf8mb4",
            connect_timeout=10,
        )
        return self.conn

    def escape_string(self, value: str) -> str:
        """字符串参数转义"""
        if value is None:
            return ""
        return escape_string(value)

    @staticmethod
    def _check_conn_value(field, value):
        """校验拼进 goInception 会话头的连接参数。

        会话头是 /* */ 注释且值用单引号包裹：值里出现 `*/` 或换行就会提前结束
        注释、把后面的内容当作 SQL 执行；单引号会提前结束值本身，最坏可借机注入
        额外会话选项（如 --execute）。goInception 对引号内的转义规则未知，
        无法安全转义，只能 fail-closed，避免把配置问题放大成注入。
        """
        text = "" if value is None else str(value)
        if "*/" in text or "'" in text or "\n" in text or "\r" in text:
            raise ValueError(
                f"实例连接信息 {field} 含单引号、*/ 或换行，无法提交 goInception，"
                "请修改实例连接信息后重试"
            )
        return text

    @classmethod
    def _conn_header(cls, host, port, user, password, options=""):
        """构造 goInception 的目标实例连接头注释。"""
        user = cls._check_conn_value("user", user)
        password = cls._check_conn_value("password", password)
        host = cls._check_conn_value("host", host)
        return (
            f"/*--user='{user}';--password='{password}';--host='{host}';"
            f"--port={int(port)};{options}*/"
        )

    def execute_check(self, instance=None, db_name=None, sql=""):
        """inception check"""
        # 判断如果配置了隧道则连接隧道
        host, port, user, password = self.remote_instance_conn(instance)
        check_result = ReviewSet(full_sql=sql)
        # inception 校验
        check_result.rows = []
        variables, set_session_sql = get_session_variables(instance)
        # 获取real_row_count参数选项
        real_row_count = SysConfig().get("real_row_count", False)
        real_row_count_option = "--real_row_count=true;" if real_row_count else ""
        conn_header = self._conn_header(
            host, port, user, password, f"--check=1;{real_row_count_option}"
        )
        inception_sql = f"""{conn_header}
                            inception_magic_start;
                            {set_session_sql}
                            use {quote_db_ident(db_name)};
                            {sql.rstrip(';')};
                            inception_magic_commit;"""
        inception_result = self.query(sql=inception_sql)
        check_result.syntax_type = (
            2  # TODO 工单类型 0、其他 1、DDL，2、DML 仅适用于MySQL，待调整
        )
        for r in inception_result.rows:
            check_result.rows += [ReviewResult(inception_result=r)]
            if r[2] == 1:  # 警告
                check_result.warning_count += 1
            elif r[2] == 2:  # 错误
                check_result.error_count += 1
            # 没有找出DDL语句的才继续执行此判断
            if check_result.syntax_type == 2:
                if get_syntax_type(r[5], parser=False, db_type="mysql") == "DDL":
                    check_result.syntax_type = 1
        check_result.column_list = inception_result.column_list
        check_result.checked = True
        check_result.error = inception_result.error
        check_result.warning = inception_result.warning
        return check_result

    def execute(self, workflow=None):
        """执行上线单"""
        instance = workflow.instance
        # 判断如果配置了隧道则连接隧道
        host, port, user, password = self.remote_instance_conn(instance)
        execute_result = ReviewSet(full_sql=workflow.sqlworkflowcontent.sql_content)
        if workflow.is_backup:
            str_backup = "--backup=1"
        else:
            str_backup = "--backup=0"

        # 提交inception执行
        variables, set_session_sql = get_session_variables(instance)
        conn_header = self._conn_header(
            host,
            port,
            user,
            password,
            f"--execute=1;--ignore-warnings=1;{str_backup};"
            f"--sleep=200;--sleep_rows=100",
        )
        sql_execute = f"""{conn_header}
                            inception_magic_start;
                            {set_session_sql}
                            use {quote_db_ident(workflow.db_name)};
                            {workflow.sqlworkflowcontent.sql_content.rstrip(';')};
                            inception_magic_commit;"""
        inception_result = self.query(sql=sql_execute)
        # 执行报错，inception crash或者执行中连接异常的场景
        if inception_result.error and not execute_result.rows:
            execute_result.error = inception_result.error
            execute_result.rows = [
                ReviewResult(
                    stage="Execute failed",
                    errlevel=2,
                    stagestatus="异常终止",
                    errormessage=f"goInception Error: {inception_result.error}",
                    sql=workflow.sqlworkflowcontent.sql_content,
                )
            ]
            return execute_result

        # 把结果转换为ReviewSet
        for r in inception_result.rows:
            execute_result.rows += [ReviewResult(inception_result=r)]

        # 如果发现任何一个行执行结果里有errLevel为1或2，并且状态列没有包含Execute Successfully，则最终执行结果为有异常.
        for r in execute_result.rows:
            if r.errlevel in (1, 2) and not re.search(
                r"Execute Successfully", r.stagestatus
            ):
                execute_result.error = "Line {0} has error/warning: {1}".format(
                    r.id, r.errormessage
                )
                break
        return execute_result

    def query(self, db_name=None, sql="", limit_num=0, close_conn=True, **kwargs):
        """返回 ResultSet"""
        result_set = ResultSet(full_sql=sql)
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            effect_row = cursor.execute(sql)
            if int(limit_num) > 0:
                rows = cursor.fetchmany(size=int(limit_num))
            else:
                rows = cursor.fetchall()
            fields = cursor.description

            result_set.column_list = [i[0] for i in fields] if fields else []
            result_set.rows = rows
            result_set.affected_rows = effect_row
        except Exception as e:
            logger.warning(f"goInception语句执行报错，错误信息{traceback.format_exc()}")
            result_set.error = str(e)
        if close_conn:
            self.close()
        return result_set

    def query_print(self, instance, db_name=None, sql=""):
        """
        打印语法树。
        """
        # 判断如果配置了隧道则连接隧道
        host, port, user, password = self.remote_instance_conn(instance)
        conn_header = self._conn_header(
            host, port, user, password, "--enable-query-print;"
        )
        sql = f"""{conn_header}
                          inception_magic_start;\
                          use {quote_db_ident(db_name)};
                          {sql.rstrip(';')};
                          inception_magic_commit;"""
        print_info = self.query(db_name=db_name, sql=sql).to_dict()[1]
        if print_info.get("errmsg"):
            raise RuntimeError(print_info.get("errmsg"))
        return print_info

    def query_data_masking(self, instance, db_name=None, sql=""):
        """
        将sql交给goInception打印语法树，获取select list
        使用 masking 参数，可参考 https://github.com/hanchuanchuan/goInception/pull/355
        """
        # 判断如果配置了隧道则连接隧道
        host, port, user, password = self.remote_instance_conn(instance)
        conn_header = self._conn_header(host, port, user, password, "--masking=1;")
        sql = f"""{conn_header}
                          inception_magic_start;
                          use {quote_db_ident(db_name)};
                          {sql}
                          inception_magic_commit;"""
        query_result = self.query(db_name=db_name, sql=sql)
        # 有异常时主动抛出
        if query_result.error:
            raise RuntimeError(f"Inception Error: {query_result.error}")
        if not query_result.rows:
            raise RuntimeError(f"Inception Error: 未获取到语法信息")
        # 兼容某些异常场景下返回内容为审核结果的问题 https://github.com/hhyo/Archery/issues/1826
        print_info = query_result.to_dict()[0]
        if "error_level" in print_info:
            raise RuntimeError(f'Inception Error: {print_info.get("error_message")}')
        if print_info.get("errlevel") == 0 and print_info.get("errmsg") is None:
            return json.loads(print_info["query_tree"])
        else:
            raise RuntimeError(f'Inception Error: print_info.get("errmsg")')

    def get_rollback(self, workflow):
        """
        获取回滚语句，并且按照执行顺序倒序展示，return ['源语句'，'回滚语句']
        """
        list_execute_result = json.loads(
            workflow.sqlworkflowcontent.execute_result or "[]"
        )
        # 回滚语句倒序展示
        list_execute_result.reverse()
        list_backup_sql = []
        # 创建连接
        conn = self.get_backup_connection()
        cur = conn.cursor()
        for row in list_execute_result:
            try:
                # 获取backup_db_name， 兼容旧数据'[[]]'格式
                if isinstance(row, list):
                    if row[8] == "None":
                        continue
                    backup_db_name = row[8]
                    sequence = row[7]
                    sql = row[5]
                # 新数据
                else:
                    if row.get("backup_dbname") in ("None", ""):
                        continue
                    backup_db_name = row.get("backup_dbname")
                    sequence = row.get("sequence")
                    sql = row.get("sql")
                # 获取备份表名
                opid_time = sequence.replace("'", "")
                sql_table = f"""select tablename
                                from {backup_db_name}.$_$Inception_backup_information$_$
                                where opid_time='{opid_time}';"""

                cur.execute(sql_table)
                list_tables = cur.fetchall()
                if list_tables:
                    # 获取备份语句
                    table_name = list_tables[0][0]
                    sql_back = f"""select rollback_statement
                                   from {backup_db_name}.{table_name}
                                   where opid_time='{opid_time}'"""
                    cur.execute(sql_back)
                    list_backup = cur.fetchall()
                    # 拼接成回滚语句列表,['源语句'，'回滚语句']
                    list_backup_sql.append(
                        [sql, "\n".join([back_info[0] for back_info in list_backup])]
                    )
            except Exception as e:
                logger.error(f"获取回滚语句报错，异常信息{traceback.format_exc()}")
                raise Exception(e)
        # 关闭连接
        if conn:
            conn.close()
        return list_backup_sql

    def get_variables(self, variables=None):
        """获取实例参数"""
        if variables:
            sql = f"inception get variables like '{variables[0]}';"
        else:
            sql = "inception get variables;"
        return self.query(sql=sql)

    def set_variable(self, variable_name, variable_value):
        """修改实例参数值"""
        sql = f"""inception set {variable_name}={variable_value};"""
        return self.query(sql=sql)

    def osc_control(self, **kwargs):
        """控制osc执行，获取进度、终止、暂停、恢复等"""
        sqlsha1 = self.escape_string(kwargs.get("sqlsha1", ""))
        command = self.escape_string(kwargs.get("command", ""))
        if command == "get":
            sql = f"inception get osc_percent '{sqlsha1}';"
        else:
            sql = f"inception {command} osc '{sqlsha1}';"
        return self.query(sql=sql)

    @staticmethod
    def get_table_ref(query_tree, db_name=None):
        __author__ = "xxlrr"
        """
        * 从goInception解析后的语法树里解析出兼容Inception格式的引用表信息。
        * 目前的逻辑是在SQL语法树中通过递归查找选中最小的 TableRefs 子树（可能有多个），
        然后在最小的 TableRefs 子树选中Source节点来获取表引用信息。
        * 查找最小TableRefs子树的方案竟然是通过逐步查找最大子树（直到找不到）来获得的，
        具体为什么这样实现，我不记得了，只记得当时是通过猜测goInception的语法树生成规
        则来写代码，结果猜一次错一次错一次猜一次，最终代码逐渐演变于此。或许直接查找最
        小子树才是效率较高的算法，但是就这样吧，反正它能运行 :)
        """
        table_ref = []

        find_queue = [query_tree]
        for tree in find_queue:
            tree = DictTree(tree)

            # nodes = tree.find_max_tree("TableRefs") or tree.find_max_tree("Left", "Right")
            nodes = tree.find_max_tree("TableRefs", "Left", "Right")
            if nodes:
                # assert isinstance(v, dict) is true
                find_queue.extend([v for node in nodes for v in node.values() if v])
            else:
                snodes = tree.find_max_tree("Source")
                if snodes:
                    table_ref.extend(
                        [
                            {
                                "schema": snode["Source"].get("Schema", {}).get("O")
                                or db_name,
                                "name": snode["Source"].get("Name", {}).get("O", ""),
                            }
                            for snode in snodes
                        ]
                    )
                # assert: source node must exists if table_refs node exists.
                # else:
                #     raise Exception("GoInception Error: not found source node")
        return table_ref


class DictTree(dict):
    def find_max_tree(self, *keys):
        __author__ = "xxlrr"
        """通过广度优先搜索算法查找满足条件的最大子树(不找叶子节点)"""
        fit = []
        find_queue = [self]
        for tree in find_queue:
            for k, v in tree.items():
                if k in keys:
                    fit.append({k: v})
                elif isinstance(v, dict):
                    find_queue.append(v)
                elif isinstance(v, list):
                    find_queue.extend([n for n in v if isinstance(n, dict)])
        return fit


def get_session_variables(instance):
    """按照目标实例动态设置goInception的会话参数，可用于按照业务组自定义审核规则等场景"""
    variables = {}
    set_session_sql = ""
    if AliyunRdsConfig.objects.filter(instance=instance, is_enable=True).exists():
        variables.update(
            {
                "ghost_aliyun_rds": "on",
                "ghost_allow_on_master": "true",
                "ghost_assume_rbr": "true",
            }
        )
    # 转换成SQL语句
    for k, v in variables.items():
        set_session_sql += f"inception set session {k} = '{v}';\n"
    return variables, set_session_sql
