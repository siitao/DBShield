# -*- coding: UTF-8 -*-
"""
@author: hhyo、yyukai
@license: Apache Licence
@file: pgsql.py
@time: 2019/03/29
"""

import json
import re
import psycopg2
import logging
import traceback
import sqlparse

from pglast import parse_sql
from pglast.enums import ObjectType
from pglast.ast import (
    SelectStmt,
    UpdateStmt,
    DeleteStmt,
    InsertStmt,
    DropStmt,
    TruncateStmt,
    CreateStmt,
    AlterTableStmt,
)

from common.config import SysConfig
from common.utils.timer import FuncTimer
from sql.utils.sql_utils import get_syntax_type
from . import EngineBase
from .models import ResultSet, ReviewSet, ReviewResult
from sql.utils.data_masking import simple_column_mask

__author__ = "hhyo、yyukai"

logger = logging.getLogger("default")


class PgSQLEngine(EngineBase):
    test_query = "SELECT 1"

    def get_connection(self, db_name=None):
        db_name = db_name or self.db_name or "postgres"
        if self.conn:
            return self.conn
        self.conn = psycopg2.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            client_encoding=self.instance.charset,
            dbname=db_name,
            connect_timeout=10,
        )
        return self.conn

    name = "PgSQL"

    info = "PgSQL engine"

    def get_all_databases(self):
        """
        获取数据库列表
        :return:
        """
        result = self.query(sql=f"SELECT datname FROM pg_database;")
        db_list = [
            row[0] for row in result.rows if row[0] not in ["template0", "template1"]
        ]
        result.rows = db_list
        return result

    def get_all_schemas(self, db_name, **kwargs):
        """
        获取模式列表
        :return:
        """
        result = self.query(
            db_name=db_name, sql=f"select schema_name from information_schema.schemata;"
        )
        schema_list = [
            row[0]
            for row in result.rows
            if row[0]
            not in [
                "information_schema",
                "pg_catalog",
                "pg_toast_temp_1",
                "pg_temp_1",
                "pg_toast",
            ]
        ]
        result.rows = schema_list
        return result

    def get_all_tables(self, db_name, **kwargs):
        """
        获取表列表
        :param db_name:
        :param schema_name:
        :return:
        """
        schema_name = kwargs.get("schema_name")
        sql = f"""SELECT table_name
        FROM information_schema.tables
        where table_schema =%(schema_name)s;"""
        result = self.query(
            db_name=db_name, sql=sql, parameters={"schema_name": schema_name}
        )
        tb_list = [row[0] for row in result.rows if row[0] not in ["test"]]
        result.rows = tb_list
        return result

    def get_all_columns_by_tb(self, db_name, tb_name, **kwargs):
        """
        获取字段列表
        :param db_name:
        :param tb_name:
        :param schema_name:
        :return:
        """
        schema_name = kwargs.get("schema_name")
        sql = f"""SELECT column_name
        FROM information_schema.columns
        where table_name=%(tb_name)s
        and table_schema=%(schema_name)s;"""
        result = self.query(
            db_name=db_name,
            sql=sql,
            parameters={"schema_name": schema_name, "tb_name": tb_name},
        )
        column_list = [row[0] for row in result.rows]
        result.rows = column_list
        return result

    def describe_table(self, db_name, tb_name, **kwargs):
        """
        获取表结构信息
        :param db_name:
        :param tb_name:
        :param schema_name:
        :return:
        """
        schema_name = kwargs.get("schema_name")
        sql = f"""select
        col.column_name,
        col.data_type,
        col.character_maximum_length,
        col.numeric_precision,
        col.numeric_scale,
        col.is_nullable,
        col.column_default,
        des.description
        from
        information_schema.columns col left join pg_description des on
        col.table_name::regclass = des.objoid
        and col.ordinal_position = des.objsubid
        where table_name = %(tb_name)s
        and col.table_schema = %(schema_name)s
        order by ordinal_position;"""
        result = self.query(
            db_name=db_name,
            schema_name=schema_name,
            sql=sql,
            parameters={"schema_name": schema_name, "tb_name": tb_name},
        )
        return result

    def query_check(self, db_name=None, sql=""):
        # 查询语句的检查、注释去除、切分
        result = {"msg": "", "bad_query": False, "filtered_sql": sql, "has_star": False}
        # 删除注释语句，进行语法判断，执行第一条有效sql
        try:
            sql = sqlparse.format(sql, strip_comments=True)
            sql = sqlparse.split(sql)[0]
            result["filtered_sql"] = sql.strip()
        except IndexError:
            result["bad_query"] = True
            result["msg"] = "没有有效的SQL语句"
        if re.match(r"^select|^explain", sql, re.I) is None:
            result["bad_query"] = True
            result["msg"] = "不支持的查询语法类型!"
        if "*" in sql:
            result["has_star"] = True
            result["msg"] += "SQL语句中含有 * "
        return result

    def query(
        self,
        db_name=None,
        sql="",
        limit_num=0,
        close_conn=True,
        parameters=None,
        **kwargs,
    ):
        """返回 ResultSet"""
        schema_name = kwargs.get("schema_name")
        result_set = ResultSet(full_sql=sql)
        try:
            conn = self.get_connection(db_name=db_name)
            conn.autocommit = False
            max_execution_time = kwargs.get("max_execution_time", 0)
            cursor = conn.cursor()
            try:
                cursor.execute(f"SET statement_timeout TO {max_execution_time};")
            except:
                pass
            cursor.execute("SET transaction ISOLATION LEVEL READ COMMITTED READ ONLY;")
            if schema_name:
                cursor.execute(
                    f"SET search_path TO %(schema_name)s;", {"schema_name": schema_name}
                )
            cursor.execute(sql, parameters)
            # effect_row = cursor.rowcount
            if int(limit_num) > 0:
                rows = cursor.fetchmany(size=int(limit_num))
            else:
                rows = cursor.fetchall()
            conn.commit()
            fields = cursor.description
            column_type_codes = [i[1] for i in fields] if fields else []
            # 定义 JSON 和 JSONB 的 type_code,# 114 是 json，3802 是 jsonb
            JSON_TYPE_CODE = 114
            JSONB_TYPE_CODE = 3802
            # 对 rows 进行循环处理，判断是否是 jsonb 或 json 类型
            converted_rows = []
            for row in rows:
                new_row = []
                for idx, col_value in enumerate(row):
                    # 理论上, 下标不会越界的
                    column_type_code = (
                        column_type_codes[idx] if idx < len(column_type_codes) else None
                    )
                    # 只在列类型为 json 或 jsonb 时转换
                    if column_type_code in [JSON_TYPE_CODE, JSONB_TYPE_CODE]:
                        if isinstance(col_value, (dict, list)):
                            new_row.append(
                                json.dumps(col_value, ensure_ascii=False)
                            )  # 转为 JSON 字符串
                        else:
                            new_row.append(col_value)
                    else:
                        new_row.append(col_value)
                converted_rows.append(tuple(new_row))

            result_set.column_list = [i[0] for i in fields] if fields else []
            result_set.rows = converted_rows
            result_set.affected_rows = len(converted_rows)
        except Exception as e:
            conn.rollback()
            logger.warning(
                f"PgSQL命令执行报错，语句：{sql}， 错误信息：{traceback.format_exc()}"
            )
            result_set.error = str(e)
        finally:
            if close_conn:
                self.close()
        return result_set

    def filter_sql(self, sql="", limit_num=0):
        # 对查询sql增加limit限制，# TODO limit改写待优化
        sql_lower = sql.lower().rstrip(";").strip()
        if re.match(r"^select", sql_lower):
            if re.search(r"limit\s+(\d+)$", sql_lower) is None:
                if re.search(r"limit\s+\d+\s*,\s*(\d+)$", sql_lower) is None:
                    return f"{sql.rstrip(';')} limit {limit_num};"
        return f"{sql.rstrip(';')};"

    def query_masking(self, db_name=None, sql="", resultset=None):
        """简单字段脱敏规则, 仅对select有效"""
        if re.match(r"^select", sql, re.I):
            filtered_result = simple_column_mask(self.instance, resultset)
            filtered_result.is_masked = True
        else:
            filtered_result = resultset
        return filtered_result

    def execute_check(self, db_name=None, sql="", run_ai_review=True, ai_user_name=""):
        """上线单执行前的检查, 返回Review set

        基于 pglast（PostgreSQL 原生语法树）做 AST 级审核：
        - 语法错误直接拦截（errlevel=2）
        - SELECT/EXPLAIN 禁止走工单（errlevel=2）
        - DROP TABLE / TRUNCATE 视为高危，按配置 critical_ddl_regex 拦截（errlevel=2）
        - UPDATE/DELETE 缺少 WHERE 子句告警（errlevel=1）
        - 命中 critical_ddl_regex 的语句拦截（errlevel=2）
        - 其余正常放行（errlevel=0）

        :param run_ai_review: 是否触发 AI 风险审核（纯参考不阻断，批量评审与
            降级口径见 sql/utils/ai_review.py）；提交工单时由调用方传 False
        :param ai_user_name: 触发检测的用户名，透传给 AI 用量记账
        """
        config = SysConfig()
        check_result = ReviewSet(full_sql=sql)
        critical_ddl_regex = config.get("critical_ddl_regex", "")
        p = re.compile(critical_ddl_regex) if critical_ddl_regex else None
        check_result.syntax_type = 2  # 工单类型 0、其他 1、DDL，2、DML
        line = 1
        for statement in sqlparse.split(sql):
            statement = sqlparse.format(statement, strip_comments=True)
            if not statement.strip():
                line += 1
                continue
            result = self._check_single_statement(
                statement, line, p, critical_ddl_regex
            )
            # 判断工单类型：DDL 则升级为 1
            if (
                get_syntax_type(statement, db_type="pgsql") == "DDL"
                and check_result.syntax_type == 2
            ):
                check_result.syntax_type = 1
            check_result.rows += [result]
            line += 1
        # 统计警告和错误数量
        for r in check_result.rows:
            if r.errlevel == 1:
                check_result.warning_count += 1
            if r.errlevel == 2:
                check_result.error_count += 1
        # AI 风险审核（纯参考，不改 errlevel；任何异常均静默降级为 unknown）
        if run_ai_review:
            from sql.utils import ai_review

            ai_review.run_ai_review(
                self, check_result, "pgsql", db_name, user_name=ai_user_name
            )
        return check_result

    def _check_single_statement(self, statement, line, critical_regex, critical_ddl_regex):
        """对单条 SQL 做 AST 级审核，返回 ReviewResult

        :param critical_regex: 已编译的 critical_ddl_regex（可能为 None）
        :param critical_ddl_regex: 原始 critical_ddl_regex 字符串（用于错误提示）
        """
        stmt_lower = statement.strip().lower()
        # 1. 禁用查询语句（select/explain）走工单
        if re.match(r"^(select|explain)\b", stmt_lower):
            return ReviewResult(
                id=line,
                errlevel=2,
                stagestatus="驳回不支持语句",
                errormessage="仅支持DML和DDL语句，查询语句请使用SQL查询功能！",
                sql=statement,
            )
        # 2. 基于 pglast AST 审核
        ast_node = self._parse_pg_statement(statement, line)
        if ast_node is None:
            # 解析失败已在 _parse_pg_statement 中返回错误结果
            return ReviewResult(
                id=line,
                errlevel=2,
                stagestatus="驳回不支持语句",
                errormessage="语法错误，无法解析该SQL语句，请检查语法！",
                sql=statement,
            )
        # 3. DROP TABLE / TRUNCATE 高危拦截
        if isinstance(ast_node, DropStmt):
            if ast_node.removeType == ObjectType.OBJECT_TABLE:
                return ReviewResult(
                    id=line,
                    errlevel=2,
                    stagestatus="驳回高危SQL",
                    errormessage="禁止删除表操作(DROP TABLE)，请确认后提交！",
                    sql=statement,
                )
        if isinstance(ast_node, TruncateStmt):
            return ReviewResult(
                id=line,
                errlevel=2,
                stagestatus="驳回高危SQL",
                errormessage="禁止清空表操作(TRUNCATE)，请确认后提交！",
                sql=statement,
            )
        # 4. 命中自定义高危正则
        if critical_regex and critical_regex.search(stmt_lower):
            return ReviewResult(
                id=line,
                errlevel=2,
                stagestatus="驳回高危SQL",
                errormessage="禁止提交匹配" + critical_ddl_regex + "条件的语句！",
                sql=statement,
            )
        # 5. UPDATE/DELETE 缺少 WHERE 子句告警
        if isinstance(ast_node, (UpdateStmt, DeleteStmt)):
            if ast_node.whereClause is None:
                return ReviewResult(
                    id=line,
                    errlevel=1,
                    stagestatus="警告",
                    errormessage="UPDATE/DELETE语句未指定WHERE条件，可能影响全表数据，请确认！",
                    sql=statement,
                    affected_rows=0,
                    execute_time=0,
                )
        # 6. 正常放行
        return ReviewResult(
            id=line,
            errlevel=0,
            stagestatus="Audit completed",
            errormessage="None",
            sql=statement,
            affected_rows=0,
            execute_time=0,
        )

    def _parse_pg_statement(self, statement, line):
        """用 pglast 解析单条 SQL，返回首个语句节点；解析失败返回 None"""
        try:
            stmts = parse_sql(statement)
            return stmts[0].stmt if stmts else None
        except Exception as e:
            logger.warning(f"PG SQL解析失败，语句：{statement}，错误：{e}")
            return None

    def execute_workflow(self, workflow, close_conn=True):
        """执行上线单，返回Review set"""
        sql = workflow.sqlworkflowcontent.sql_content
        execute_result = ReviewSet(full_sql=sql)
        # 删除注释语句，切分语句，将切换CURRENT_SCHEMA语句增加到切分结果中
        sql = sqlparse.format(sql, strip_comments=True)
        split_sql = sqlparse.split(sql)
        line = 1
        statement = None
        db_name = workflow.db_name
        try:
            conn = self.get_connection(db_name=db_name)
            conn.autocommit = False
            cursor = conn.cursor()
            cursor.execute("SET transaction ISOLATION LEVEL READ COMMITTED READ WRITE;")
            # 逐条执行切分语句，追加到执行结果中
            for statement in split_sql:
                statement = statement.rstrip(";")
                with FuncTimer() as t:
                    cursor.execute(statement)
                execute_result.rows.append(
                    ReviewResult(
                        id=line,
                        errlevel=0,
                        stagestatus="Execute Successfully",
                        errormessage="None",
                        sql=statement,
                        affected_rows=cursor.rowcount,
                        execute_time=t.cost,
                    )
                )
                line += 1
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.warning(
                f"PGSQL命令执行报错，语句：{statement or sql}， 错误信息：{traceback.format_exc()}"
            )
            execute_result.error = str(e)
            # 追加当前报错语句信息到执行结果中
            execute_result.rows.append(
                ReviewResult(
                    id=line,
                    errlevel=2,
                    stagestatus="Execute Failed",
                    errormessage=f"异常信息：{e}",
                    sql=statement or sql,
                    affected_rows=0,
                    execute_time=0,
                )
            )
            line += 1
            # 报错语句后面的语句标记为审核通过、未执行，追加到执行结果中
            for statement in split_sql[line - 1 :]:
                execute_result.rows.append(
                    ReviewResult(
                        id=line,
                        errlevel=0,
                        stagestatus="Audit completed",
                        errormessage=f"前序语句失败, 未执行",
                        sql=statement,
                        affected_rows=0,
                        execute_time=0,
                    )
                )
                line += 1
        finally:
            if close_conn:
                self.close()
        return execute_result

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    # ==================== 数据字典：表元信息 ====================

    def get_table_meta_data(self, db_name, tb_name, **kwargs):
        """获取表元信息，返回 {column_list, rows}（rows 取首行）"""
        schema_name = kwargs.get("schema_name", "public")
        sql = """SELECT
                    c.relname AS table_name,
                    pg_get_userbyid(c.relowner) AS owner,
                    c.reltuples::bigint AS table_rows,
                    pg_table_size(c.oid) AS data_length,
                    pg_indexes_size(c.oid) AS index_length,
                    (pg_table_size(c.oid) + pg_indexes_size(c.oid)) AS data_total,
                    c.relhaspkey AS has_primary_key,
                    c.relpersistence AS persistence,
                    obj_description(c.oid, 'pg_class') AS table_comment
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind = 'r'
                    AND n.nspname = %(schema_name)s
                    AND c.relname = %(tb_name)s;"""
        _meta = self.query(
            db_name=db_name,
            sql=sql,
            parameters={"schema_name": schema_name, "tb_name": tb_name},
        )
        return {
            "column_list": _meta.column_list,
            "rows": _meta.rows[0] if _meta.rows else [],
        }

    def get_table_desc_data(self, db_name, tb_name, **kwargs):
        """获取表字段信息，返回 {column_list, rows}"""
        schema_name = kwargs.get("schema_name", "public")
        sql = """SELECT
                    column_name AS "列名",
                    data_type AS "数据类型",
                    character_maximum_length AS "长度",
                    is_nullable AS "是否为空",
                    column_default AS "默认值",
                    col_description(
                        (SELECT oid FROM pg_catalog.pg_class WHERE relname = %(tb_name)s
                         AND relnamespace = (SELECT oid FROM pg_catalog.pg_namespace WHERE nspname = %(schema_name)s)),
                        ordinal_position) AS "列说明"
                FROM information_schema.columns
                WHERE table_schema = %(schema_name)s
                    AND table_name = %(tb_name)s
                ORDER BY ordinal_position;"""
        _desc = self.query(
            db_name=db_name,
            sql=sql,
            parameters={"schema_name": schema_name, "tb_name": tb_name},
        )
        return {"column_list": _desc.column_list, "rows": _desc.rows}

    def get_table_index_data(self, db_name, tb_name, **kwargs):
        """获取表索引信息，返回 {column_list, rows}"""
        schema_name = kwargs.get("schema_name", "public")
        sql = """SELECT
                    pg_get_indexdef(i.indexrelid) AS index_def
                FROM pg_index i
                JOIN pg_class c ON c.oid = i.indrelid
                JOIN pg_class ci ON ci.oid = i.indexrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %(schema_name)s
                    AND c.relname = %(tb_name)s;"""
        _idx = self.query(
            db_name=db_name,
            sql=sql,
            parameters={"schema_name": schema_name, "tb_name": tb_name},
        )
        return {"column_list": _idx.column_list, "rows": _idx.rows}

    def get_tables_metas_data(self, db_name, **kwargs):
        """获取数据库所有表元信息，用于数据字典导出，返回 list"""
        schema_name = kwargs.get("schema_name", "public")
        tables_result = self.get_all_tables(db_name, schema_name=schema_name)
        metas = []
        for tb_name in tables_result.rows:
            metas.append(
                {
                    "ENGINE_KEYS": [
                        {"key": "表名", "value": tb_name},
                        {
                            "key": "元信息",
                            "value": self.get_table_meta_data(
                                db_name, tb_name, schema_name=schema_name
                            ),
                        },
                        {
                            "key": "字段信息",
                            "value": self.get_table_desc_data(
                                db_name, tb_name, schema_name=schema_name
                            ),
                        },
                        {
                            "key": "索引信息",
                            "value": self.get_table_index_data(
                                db_name, tb_name, schema_name=schema_name
                            ),
                        },
                    ],
                    "TABLE_INFO": tb_name,
                    "COLUMNS": self.get_table_desc_data(
                        db_name, tb_name, schema_name=schema_name
                    ).get("rows", []),
                }
            )
        return metas

    # ==================== 数据字典：视图 ====================

    def get_views_list(self, db_name, **kwargs):
        """获取视图列表，按首字符分组"""
        schema_name = kwargs.get("schema_name", "public")
        data = {}
        sql = """SELECT viewname, definition
                FROM pg_views
                WHERE schemaname = %(schema_name)s;"""
        result = self.query(
            db_name=db_name, sql=sql, parameters={"schema_name": schema_name}
        )
        for row in result.rows:
            view_name = row[0]
            view_def = (row[1] or "")[:80]
            first_char = view_name[0] if view_name else "#"
            data.setdefault(first_char, []).append([view_name, view_def])
        return data

    def get_view_detail(self, db_name, view_name, **kwargs):
        """获取视图详情"""
        schema_name = kwargs.get("schema_name", "public")
        sql = """SELECT
                    schemaname AS schema_name,
                    viewname AS view_name,
                    viewowner AS owner,
                    definition AS view_definition
                FROM pg_views
                WHERE schemaname = %(schema_name)s AND viewname = %(view_name)s;"""
        _meta = self.query(
            db_name=db_name,
            sql=sql,
            parameters={"schema_name": schema_name, "view_name": view_name},
        )
        meta_data = {
            "column_list": _meta.column_list,
            "rows": _meta.rows[0] if _meta.rows else [],
        }
        view_definition = ""
        if _meta.rows:
            view_definition = _meta.rows[0][3] or ""
        desc = self.get_table_desc_data(
            db_name=db_name, tb_name=view_name, schema_name=schema_name
        )
        return {"meta_data": meta_data, "desc": desc, "view_definition": view_definition}

    # ==================== 数据字典：函数 ====================

    def get_functions_list(self, db_name, **kwargs):
        """获取函数列表，按首字符分组"""
        schema_name = kwargs.get("schema_name", "public")
        data = {}
        # prokind='f' 表示函数（PG11+）
        sql = """SELECT p.proname,
                    pg_get_function_result(p.oid)
                FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname = %(schema_name)s AND p.prokind = 'f';"""
        result = self.query(
            db_name=db_name, sql=sql, parameters={"schema_name": schema_name}
        )
        for row in result.rows:
            func_name = row[0]
            ret_type = row[1] or ""
            desc = f"返回类型: {ret_type}"
            first_char = func_name[0] if func_name else "#"
            data.setdefault(first_char, []).append([func_name, desc])
        return data

    def get_function_detail(self, db_name, func_name, **kwargs):
        """获取函数详情"""
        schema_name = kwargs.get("schema_name", "public")
        sql = """SELECT
                    n.nspname AS schema_name,
                    p.proname AS routine_name,
                    pg_Get_function_result(p.oid) AS return_type,
                    pg_get_function_arguments(p.oid) AS arguments,
                    pg_catalog.obj_description(p.oid, 'pg_proc') AS comment,
                    l.lanname AS language,
                    pg_get_userbyid(p.proowner) AS owner,
                    pg_get_functiondef(p.oid) AS create_sql
                FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                JOIN pg_language l ON l.oid = p.prolang
                WHERE n.nspname = %(schema_name)s
                    AND p.proname = %(func_name)s
                    AND p.prokind = 'f';"""
        _meta = self.query(
            db_name=db_name,
            sql=sql,
            parameters={"schema_name": schema_name, "func_name": func_name},
        )
        meta_data = {
            "column_list": _meta.column_list,
            "rows": _meta.rows[0] if _meta.rows else [],
        }
        create_sql = ""
        if _meta.rows:
            # create_sql 在最后一列
            create_sql = _meta.rows[0][-1] or ""
        return {"meta_data": meta_data, "create_sql": [create_sql]}

    # ==================== 数据字典：存储过程 ====================

    def get_procedures_list(self, db_name, **kwargs):
        """获取存储过程列表，按首字符分组"""
        schema_name = kwargs.get("schema_name", "public")
        data = {}
        # prokind='p' 表示过程（PG11+）
        sql = """SELECT p.proname
                FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname = %(schema_name)s AND p.prokind = 'p';"""
        result = self.query(
            db_name=db_name, sql=sql, parameters={"schema_name": schema_name}
        )
        for row in result.rows:
            proc_name = row[0]
            first_char = proc_name[0] if proc_name else "#"
            data.setdefault(first_char, []).append([proc_name, ""])
        return data

    def get_procedure_detail(self, db_name, proc_name, **kwargs):
        """获取存储过程详情"""
        schema_name = kwargs.get("schema_name", "public")
        sql = """SELECT
                    n.nspname AS schema_name,
                    p.proname AS routine_name,
                    pg_get_function_arguments(p.oid) AS arguments,
                    pg_catalog.obj_description(p.oid, 'pg_proc') AS comment,
                    l.lanname AS language,
                    pg_get_userbyid(p.proowner) AS owner,
                    pg_get_functiondef(p.oid) AS create_sql
                FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                JOIN pg_language l ON l.oid = p.prolang
                WHERE n.nspname = %(schema_name)s
                    AND p.proname = %(proc_name)s
                    AND p.prokind = 'p';"""
        _meta = self.query(
            db_name=db_name,
            sql=sql,
            parameters={"schema_name": schema_name, "proc_name": proc_name},
        )
        meta_data = {
            "column_list": _meta.column_list,
            "rows": _meta.rows[0] if _meta.rows else [],
        }
        create_sql = ""
        if _meta.rows:
            create_sql = _meta.rows[0][-1] or ""
        return {"meta_data": meta_data, "create_sql": [create_sql]}

    # ==================== 数据字典：触发器 ====================

    def get_triggers_list(self, db_name, **kwargs):
        """获取触发器列表，按首字符分组"""
        schema_name = kwargs.get("schema_name", "public")
        data = {}
        sql = """SELECT
                    t.tgname,
                    pg_get_triggerdef(t.oid)
                FROM pg_trigger t
                JOIN pg_class c ON c.oid = t.tgrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %(schema_name)s
                    AND NOT t.tgisinternal;"""
        result = self.query(
            db_name=db_name, sql=sql, parameters={"schema_name": schema_name}
        )
        for row in result.rows:
            trig_name = row[0]
            trig_def = (row[1] or "")[:80]
            first_char = trig_name[0] if trig_name else "#"
            data.setdefault(first_char, []).append([trig_name, trig_def])
        return data

    def get_trigger_detail(self, db_name, trigger_name, **kwargs):
        """获取触发器详情"""
        schema_name = kwargs.get("schema_name", "public")
        sql = """SELECT
                    t.tgname AS trigger_name,
                    pg_get_triggerdef(t.oid) AS trigger_definition
                FROM pg_trigger t
                JOIN pg_class c ON c.oid = t.tgrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %(schema_name)s
                    AND t.tgname = %(trigger_name)s
                    AND NOT t.tgisinternal;"""
        _data = self.query(
            db_name=db_name,
            sql=sql,
            parameters={"schema_name": schema_name, "trigger_name": trigger_name},
        )
        return {
            "column_list": _data.column_list,
            "rows": _data.rows[0] if _data.rows else [],
        }

    # ==================== 数据字典：事件 ====================

    def get_events_list(self, db_name, **kwargs):
        """获取定时任务列表。PG 无原生事件调度，返回空字典。

        pg_cron 扩展可后续支持，暂返回空。
        """
        return {}

    def get_event_detail(self, db_name, event_name, **kwargs):
        """获取定时任务详情。PG 无原生事件调度，返回空字典。"""
        return {}

    def processlist(self, command_type, **kwargs):
        """获取连接信息"""
        sql = """
            select psa.pid
                                ,concat('{',array_to_string(pg_blocking_pids(psa.pid),','),'}') block_pids
                                ,psa.leader_pid
                                ,psa.datname,psa.usename
                                ,psa.application_name
                                ,psa.state
                                ,psa.client_addr::text client_addr
                                ,round(GREATEST(EXTRACT(EPOCH FROM (now() - psa.query_start)),0)::numeric,4) elapsed_time_seconds
                ,GREATEST(now() - psa.query_start, INTERVAL '0 second') AS elapsed_time
                        ,(case when psa.leader_pid is null then psa.query end) query
                                ,psa.wait_event_type,psa.wait_event
                                ,psa.query_start
                                ,psa.backend_start
                                ,psa.client_hostname,psa.client_port
                                ,psa.xact_start transaction_start_time
                ,psa.state_change,psa.backend_xid,psa.backend_xmin,psa.backend_type
                                from  pg_stat_activity psa
                                where 1=1
                                AND psa.pid <> pg_backend_pid()
                                $state_not_idle$
                                order by (case 
                                    when psa.state='active' then 10 
                                    when psa.state like 'idle in transaction%' then 5
                                    when psa.state='idle' then 99 else 100 end)
                                    ,elapsed_time_seconds desc
                                ,(case when psa.leader_pid is not null then 1 else 0 end);
            """
        # escape
        command_type = self.escape_string(command_type)
        if not command_type:
            command_type = "Not Idle"

        if command_type == "Not Idle":
            sql = sql.replace("$state_not_idle$", "and psa.state<>'idle'")

        # 所有的模板进行替换
        sql = sql.replace("$state_not_idle$", "")
        return self.query("postgres", sql)

    # ==================== 诊断：锁等待 / 长事务 / 终止会话 ====================

    def trxandlocks(self):
        """获取锁等待信息，返回 ResultSet

        查询 pg_locks + pg_stat_activity + pg_blocking_pids，
        展示被阻塞会话、阻塞会话及锁类型。
        """
        sql = """SELECT
                    blocked.pid        AS blocked_pid,
                    blocked.usename    AS blocked_user,
                    blocked.query      AS blocked_query,
                    blocking.pid       AS blocking_pid,
                    blocking.usename   AS blocking_user,
                    blocking.query     AS blocking_query,
                    blocked_l.relation::regclass AS locked_object,
                    blocked_l.mode     AS blocked_mode,
                    blocking_l.mode    AS blocking_mode,
                    now() - blocked.query_start AS blocked_duration
                FROM pg_stat_activity blocked
                JOIN pg_locks blocked_l
                    ON blocked_l.pid = blocked.pid AND NOT blocked_l.granted
                JOIN pg_locks blocking_l
                    ON blocking_l.relation = blocked_l.relation
                    AND blocking_l.granted
                    AND blocking_l.pid <> blocked.pid
                JOIN pg_stat_activity blocking
                    ON blocking.pid = blocking_l.pid
                WHERE blocked.pid <> pg_backend_pid();"""
        return self.query("postgres", sql)

    def get_long_transaction(self, thread_time=3):
        """获取长事务（idle in transaction 超过阈值），返回 ResultSet

        :param thread_time: 阈值分钟数，默认 3 分钟
        """
        sql = """SELECT
                    pid,
                    usename,
                    datname,
                    application_name,
                    client_addr,
                    state,
                    xact_start,
                    query_start,
                    now() - xact_start AS transaction_duration,
                    query
                FROM pg_stat_activity
                WHERE state IN ('idle in transaction', 'idle in transaction (aborted)')
                    AND xact_start IS NOT NULL
                    AND now() - xact_start > %(threshold)s::interval
                    AND pid <> pg_backend_pid()
                ORDER BY xact_start;"""
        threshold = f"{int(thread_time)} minutes"
        return self.query(
            "postgres", sql, parameters={"threshold": threshold}
        )

    def get_kill_command(self, thread_ids, thread_ids_check=True):
        """由传入的 pid 列表生成 pg_terminate_backend 命令字符串"""
        if thread_ids_check:
            if [i for i in thread_ids if not isinstance(i, int)]:
                return None
        # 校验 pid 确实存在，仅对存在的 pid 生成终止命令
        sql = "SELECT pid FROM pg_stat_activity WHERE pid = ANY(%(pids)s) AND pid <> pg_backend_pid();"
        result = self.query("postgres", sql, parameters={"pids": list(thread_ids)})
        kill_sql = ""
        for row in result.rows:
            kill_sql += f"SELECT pg_terminate_backend({row[0]});"
        return kill_sql

    def kill(self, thread_ids, thread_ids_check=True):
        """终止指定 pid 的会话"""
        if thread_ids_check:
            if [i for i in thread_ids if not isinstance(i, int)]:
                return ResultSet(full_sql="")
        kill_sql = self.get_kill_command(thread_ids, thread_ids_check=False)
        if not kill_sql:
            return ResultSet(full_sql="")
        return self.execute_db("postgres", kill_sql)

    def execute_db(self, db_name, sql, parameters=None):
        """在指定库执行原生语句（供 kill 等内部调用），返回 ResultSet"""
        result = ResultSet(full_sql=sql)
        conn = self.get_connection(db_name=db_name)
        try:
            conn.autocommit = True
            cursor = conn.cursor()
            cursor.execute(sql, parameters)
            result.column_list = (
                [desc[0] for desc in cursor.description]
                if cursor.description
                else []
            )
            result.rows = list(cursor.fetchall()) if cursor.description else []
            result.affected_rows = cursor.rowcount
        except Exception as e:
            logger.warning(f"PG命令执行报错，语句：{sql}，错误信息：{traceback.format_exc()}")
            result.error = str(e)
            conn.rollback()
        finally:
            self.close()
        return result

    # ==================== 实例参数管理 ====================

    # 这些 context 的参数需要重启才能生效，不支持在线修改
    _NEED_RESTART_CONTEXTS = {"postmaster", "superuser-backend", "internal"}

    def get_variables(self, variables=None):
        """获取实例参数，返回 ResultSet（rows 为 [[name, value], ...]）

        :param variables: 可选，指定参数名列表做过滤
        """
        if variables:
            sql = "SELECT name, setting FROM pg_settings WHERE name = ANY(%(names)s) ORDER BY name;"
            params = {"names": list(variables)}
        else:
            sql = "SELECT name, setting FROM pg_settings ORDER BY name;"
            params = None
        return self.query("postgres", sql, parameters=params)

    def set_variable(self, variable_name, variable_value):
        """修改实例参数值（ALTER SYSTEM SET），返回 ResultSet

        需重启生效的参数（context 为 postmaster 等）会被拒绝。
        执行成功后需 SELECT pg_reload_conf() 让 sighup 类参数生效，
        返回的 full_sql 供 ParamHistory 记录。
        """
        result = ResultSet()
        # 先检查该参数是否可在线修改
        check_sql = "SELECT context FROM pg_settings WHERE name = %(name)s;"
        check_result = self.query(
            "postgres", check_sql, parameters={"name": variable_name}
        )
        if check_result.error:
            result.error = check_result.error
            return result
        if not check_result.rows:
            result.error = f"参数 {variable_name} 不存在"
            return result
        context = check_result.rows[0][0]
        if context in self._NEED_RESTART_CONTEXTS:
            result.error = (
                f"参数 {variable_name} 的 context 为 {context}，"
                f"需重启 PostgreSQL 才能生效，不支持在线修改"
            )
            return result
        # 执行 ALTER SYSTEM SET
        set_sql = f'ALTER SYSTEM SET "{variable_name}" = %(value)s;'
        alter_result = self.execute_db(
            "postgres", set_sql, parameters={"value": variable_value}
        )
        result.full_sql = set_sql
        if alter_result.error:
            result.error = alter_result.error
        return result

    # ==================== 诊断：Top 表空间（表维度） ====================

    def tablespace(self, offset=0, row_count=14, schema_search=""):
        """获取表空间信息（表维度 TOP N），返回 ResultSet

        与 MySQL 的 tablespace 返回结构对齐，供诊断页前端复用。
        PG 无 data_free 概念，对应列返回 0。
        """
        search_condition = ""
        params = {}
        if schema_search:
            search_condition = (
                " AND (n.nspname ILIKE %(keyword)s OR c.relname ILIKE %(keyword)s)"
            )
            params["keyword"] = f"%{schema_search}%"
        sql = f"""SELECT
                    n.nspname    AS table_schema,
                    c.relname    AS table_name,
                    'heap'       AS engine,
                    round(pg_total_relation_size(c.oid) / 1024.0 / 1024.0, 2) AS total_size,
                    c.reltuples::bigint AS table_rows,
                    round(pg_table_size(c.oid) / 1024.0 / 1024.0, 2) AS data_size,
                    round(pg_indexes_size(c.oid) / 1024.0 / 1024.0, 2) AS index_size,
                    0.0 AS data_free,
                    0.0 AS pct_free
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind = 'r'
                    AND n.nspname NOT IN ('information_schema', 'pg_catalog', 'pg_toast'){search_condition}
                ORDER BY pg_total_relation_size(c.oid) DESC
                LIMIT %(limit)s OFFSET %(offset)s;"""
        params["limit"] = int(row_count)
        params["offset"] = int(offset)
        return self.query("postgres", sql, parameters=params)

    def tablespace_count(self, schema_search=""):
        """获取表空间（表维度）数量，返回 ResultSet"""
        search_condition = ""
        params = {}
        if schema_search:
            search_condition = (
                " AND (n.nspname ILIKE %(keyword)s OR c.relname ILIKE %(keyword)s)"
            )
            params["keyword"] = f"%{schema_search}%"
        sql = f"""SELECT count(*)
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind = 'r'
                    AND n.nspname NOT IN ('information_schema', 'pg_catalog', 'pg_toast'){search_condition};"""
        return self.query("postgres", sql, parameters=params)
