# -*- coding: UTF-8 -*-

import time

from common.utils.const import SQLTuning
from sql.engines import get_engine
from sql.models import Instance
from sql.engines.mysql import MysqlForkType
from sql.utils.sql_utils import extract_tables


class SqlTuning(object):
    def __init__(self, instance_name, db_name, sqltext):
        instance = Instance.objects.get(instance_name=instance_name)
        query_engine = get_engine(instance=instance)
        self.engine = query_engine
        self.db_name = self.engine.escape_string(db_name)
        self.sqltext = sqltext
        self.sql_variable = """
    select
      lower(variable_name),
      variable_value
    from information_schema.global_variables
    where upper(variable_name) in ('%s')
    order by variable_name;""" % ("','".join(SQLTuning.SYS_PARM_FILTER))
        self.sql_optimizer_switch = """
    select variable_value
    from information_schema.global_variables
    where upper(variable_name) = 'OPTIMIZER_SWITCH';
    """
        self.sql_table_info = """
    select
      table_name,
      engine,
      row_format                                           as format,
      table_rows,
      avg_row_length                                       as avg_row,
      round((data_length + index_length) / 1024 / 1024, 2) as total_mb,
      round((data_length) / 1024 / 1024, 2)                as data_mb,
      round((index_length) / 1024 / 1024, 2)               as index_mb
    from information_schema.tables
    where table_schema = '%s' and table_name = '%s'
    """
        self.sql_table_index = """
    select
      table_name,
      index_name,
      non_unique,
      seq_in_index,
      column_name,
      collation,
      cardinality,
      nullable,
      index_type
    from information_schema.statistics
    where table_schema = '%s' and table_name = '%s'
    order by 1, 3;    
    """

    def __extract_tables(self):
        """获取sql语句中的表名"""
        return [i["name"].strip("`") for i in extract_tables(self.sqltext)]

    def tuning(self, sqltext, option=None):
        """
        SQL 调优报告编排：按 option 维度采集并组装结果。
        option 取值与前端复选框一致：sys_parm / sql_plan / obj_stat / sql_profile
        """
        if option is None:
            option = []
        # 去除结尾分号：explain 与 profiling 执行都不需要
        self.sqltext = sqltext.strip().rstrip(";")
        result = {}
        if "sys_parm" in option:
            result["basic_information"] = self.basic_information()
            result["sys_parameter"] = self.sys_parameter()
            result["optimizer_switch"] = self.optimizer_switch()
        if "sql_plan" in option:
            plan, optimizer_rewrite_sql = self.sqlplan()
            result["plan"] = plan
            result["optimizer_rewrite_sql"] = optimizer_rewrite_sql
        if "obj_stat" in option:
            result["object_statistics"] = self.object_statistics()
        if "sql_profile" in option:
            result["session_status"] = self.exec_sql()
        result["sqltext"] = self.sqltext
        return result

    def basic_information(self):
        return self.engine.query(sql="select @@version").to_sep_dict()

    def sys_parameter(self):
        # 获取mysql版本信息
        server_version = self.engine.server_version
        if (
            self.engine.server_fork_type != MysqlForkType.MARIADB
            and server_version >= (5, 7, 6)
        ):
            sql = self.sql_variable.replace(
                "information_schema.global_variables",
                "performance_schema.global_variables",
            )
        else:
            sql = self.sql_variable
        return self.engine.query(sql=sql).to_sep_dict()

    def optimizer_switch(self):
        # 获取mysql版本信息
        server_version = self.engine.server_version
        if server_version < (5, 7, 0):
            sql = self.sql_optimizer_switch.replace(
                "performance_schema", "information_schema"
            )
        else:
            sql = self.sql_optimizer_switch
        return self.engine.query(sql=sql).to_sep_dict()

    def sqlplan(self):
        plan = self.engine.query(
            self.db_name, "explain " + self.sqltext, close_conn=False
        ).to_sep_dict()
        optimizer_rewrite_sql = self.engine.query(
            sql="show warnings", close_conn=False
        ).to_sep_dict()
        return plan, optimizer_rewrite_sql

    # 获取关联表信息存在缺陷，只能获取到一张表
    def object_statistics(self):
        object_statistics = []
        for index, table_name in enumerate(self.__extract_tables()):
            object_statistics.append(
                {
                    "structure": self.engine.query(
                        db_name=self.db_name,
                        sql=f"show create table `{table_name}`;",
                        close_conn=False,
                    ).to_sep_dict(),
                    "table_info": self.engine.query(
                        sql=self.sql_table_info % (self.db_name, table_name),
                        close_conn=False,
                    ).to_sep_dict(),
                    "index_info": self.engine.query(
                        sql=self.sql_table_index % (self.db_name, table_name),
                        close_conn=False,
                    ).to_sep_dict(),
                }
            )
        return object_statistics

    def exec_sql(self):
        result = {
            "EXECUTE_TIME": 0,
            "BEFORE_STATUS": {"column_list": [], "rows": []},
            "AFTER_STATUS": {"column_list": [], "rows": []},
            "SESSION_STATUS(DIFFERENT)": {
                "column_list": ["status_name", "before", "after", "diff"],
                "rows": [],
            },
            "PROFILING_DETAIL": {"column_list": [], "rows": []},
            "PROFILING_SUMMARY": {"column_list": [], "rows": []},
        }
        sql_profiling = """select concat(upper(left(variable_name,1)),
                            substring(lower(variable_name),
                            2,
                            (length(variable_name)-1))) var_name,
                            variable_value var_value 
                        from performance_schema.session_status order by 1"""

        # 获取mysql版本信息
        server_version = self.engine.server_version
        if server_version < (5, 7, 0):
            sql = sql_profiling.replace("performance_schema", "information_schema")
        else:
            sql = sql_profiling
        self.engine.query(sql="set profiling=1", close_conn=False).to_sep_dict()
        records = self.engine.query(
            sql="select ifnull(max(query_id),0) from INFORMATION_SCHEMA.PROFILING",
            close_conn=False,
        ).to_sep_dict()
        query_id = records["rows"][0][0] + 3  # skip next sql
        # 获取执行前信息
        result["BEFORE_STATUS"] = self.engine.query(
            sql=sql, close_conn=False
        ).to_sep_dict()

        # 执行查询语句,统计执行时间
        t_start = time.time()
        self.engine.query(sql=self.sqltext, close_conn=False).to_sep_dict()
        t_end = time.time()
        cost_time = "%5s" % "{:.4f}".format(t_end - t_start)
        result["EXECUTE_TIME"] = cost_time

        # 获取执行后信息
        result["AFTER_STATUS"] = self.engine.query(
            sql=sql, close_conn=False
        ).to_sep_dict()

        # 获取PROFILING_DETAIL信息
        result["PROFILING_DETAIL"] = self.engine.query(
            sql="select STATE,DURATION,CPU_USER,CPU_SYSTEM,BLOCK_OPS_IN,BLOCK_OPS_OUT ,MESSAGES_SENT ,MESSAGES_RECEIVED ,PAGE_FAULTS_MAJOR ,PAGE_FAULTS_MINOR ,SWAPS from INFORMATION_SCHEMA.PROFILING where query_id="
            + str(query_id)
            + " order by seq",
            close_conn=False,
        ).to_sep_dict()
        result["PROFILING_SUMMARY"] = self.engine.query(
            sql="SELECT STATE,SUM(DURATION) AS Total_R,ROUND(100*SUM(DURATION)/(SELECT SUM(DURATION) FROM INFORMATION_SCHEMA.PROFILING WHERE QUERY_ID="
            + str(query_id)
            + "),2) AS Pct_R,COUNT(*) AS Calls,SUM(DURATION)/COUNT(*) AS R_Call FROM INFORMATION_SCHEMA.PROFILING WHERE QUERY_ID="
            + str(query_id)
            + " GROUP BY STATE ORDER BY Total_R DESC",
            close_conn=False,
        ).to_sep_dict()

        # 处理执行前后对比信息
        before_status_rows = [list(item) for item in result["BEFORE_STATUS"]["rows"]]
        after_status_rows = [list(item) for item in result["AFTER_STATUS"]["rows"]]
        for index, item in enumerate(before_status_rows):
            if before_status_rows[index][1] != after_status_rows[index][1]:
                before_status_rows[index].append(after_status_rows[index][1])
                before_status_rows[index].append(
                    str(
                        float(after_status_rows[index][1])
                        - float(before_status_rows[index][1])
                    )
                )
        diff_rows = [item for item in before_status_rows if len(item) == 4]
        result["SESSION_STATUS(DIFFERENT)"]["rows"] = diff_rows
        return result
