# -*- coding: UTF-8 -*-

from datetime import timedelta
from django.db import connection


class ChartDao(object):
    # 直接在DBShield数据库查询数据，用于报表
    @staticmethod
    def __query(sql, params=None):
        cursor = connection.cursor()
        cursor.execute(sql, params or [])
        rows = cursor.fetchall()
        fields = cursor.description
        column_list = []
        if fields:
            for i in fields:
                column_list.append(i[0])
        return {"column_list": column_list, "rows": rows}

    @staticmethod
    def _group_clause(group_ids, alias=""):
        """资源组过滤片段；None=不限制，[]=无权限返回空集。"""
        if group_ids is None:
            return "", []
        if not group_ids:
            return " AND 1=0", []
        prefix = f"{alias}." if alias else ""
        placeholders = ",".join(["%s"] * len(group_ids))
        return f" AND {prefix}group_id IN ({placeholders})", list(group_ids)

    @staticmethod
    def _instance_clause(instance_names):
        """实例名过滤片段；None=不限制，[]=无权限返回空集。"""
        if instance_names is None:
            return "", []
        if not instance_names:
            return " AND 1=0", []
        placeholders = ",".join(["%s"] * len(instance_names))
        return f" AND instance_name IN ({placeholders})", list(instance_names)

    # 获取连续时间
    @staticmethod
    def get_date_list(begin_date, end_date):
        dates = []
        this_day = begin_date
        while this_day <= end_date:
            dates += [this_day.strftime("%Y-%m-%d")]
            this_day += timedelta(days=1)
        return dates

    # 语法类型
    def syntax_type(self, start_date, end_date, group_ids=None):
        clause, params = self._group_clause(group_ids)
        sql = f"""
        select
          case when syntax_type = 1
            then 'DDL'
          when syntax_type = 2
            then 'DML'
          else '其他'
          end as syntax_type,
          count(*)
        from sql_workflow 
        where create_time >= %s and create_time <= %s{clause}
        group by syntax_type;"""
        return self.__query(sql, [start_date, end_date] + params)

    # 工单数量统计
    def workflow_by_date(self, start_date, end_date, group_ids=None):
        clause, params = self._group_clause(group_ids)
        sql = f"""
        select
          date_format(create_time, '%%Y-%%m-%%d'),
          count(*)
        from sql_workflow
        where create_time >= %s and create_time <= %s{clause}
        group by date_format(create_time, '%%Y-%%m-%%d')
        order by 1 asc;"""
        return self.__query(sql, [start_date, end_date] + params)

    # 工单按组统计
    def workflow_by_group(self, start_date, end_date, group_ids=None):
        clause, params = self._group_clause(group_ids)
        sql = f"""
        select
          group_name,
          count(*)
        from sql_workflow
        where create_time >= %s and create_time <= %s{clause}
        group by group_id
        order by count(*) desc;"""
        return self.__query(sql, [start_date, end_date] + params)

    def workflow_by_user(self, start_date, end_date, group_ids=None):
        """工单按人统计"""
        # TODO select 的对象应该为engineer ID, 查询时应作联合查询查出用户中文名
        clause, params = self._group_clause(group_ids)
        sql = f"""
        select
          engineer_display,
          count(*)
        from sql_workflow
        where create_time >= %s and create_time <= %s{clause}
        group by engineer_display
        order by count(*) desc;"""
        return self.__query(sql, [start_date, end_date] + params)

    # SQL查询统计(每日检索行数)
    def querylog_effect_row_by_date(self, start_date, end_date, instance_names=None):
        clause, params = self._instance_clause(instance_names)
        sql = f"""
        select
          date_format(create_time, '%%Y-%%m-%%d'),
          sum(effect_row)
        from query_log
        where create_time >= %s and create_time <= %s{clause}
        group by date_format(create_time, '%%Y-%%m-%%d')
        order by sum(effect_row) desc;"""
        return self.__query(sql, [start_date, end_date] + params)

    # SQL查询统计(每日检索次数)
    def querylog_count_by_date(self, start_date, end_date, instance_names=None):
        clause, params = self._instance_clause(instance_names)
        sql = f"""
        select
          date_format(create_time, '%%Y-%%m-%%d'),
          count(*)
        from query_log
        where create_time >= %s and create_time <= %s{clause}
        group by date_format(create_time, '%%Y-%%m-%%d')
        order by count(*) desc;"""
        return self.__query(sql, [start_date, end_date] + params)

    # SQL查询统计(用户检索行数)
    def querylog_effect_row_by_user(self, start_date, end_date, instance_names=None):
        clause, params = self._instance_clause(instance_names)
        sql = f"""
        select 
          user_display,
          sum(effect_row)
        from query_log
        where create_time >= %s and create_time <= %s{clause}
        group by user_display
        order by sum(effect_row) desc
        limit 20;"""
        return self.__query(sql, [start_date, end_date] + params)

    # SQL查询统计(DB检索行数)
    def querylog_effect_row_by_db(self, start_date, end_date, instance_names=None):
        clause, params = self._instance_clause(instance_names)
        sql = f"""
       select
          db_name,
          sum(effect_row)
        from query_log
        where create_time >= %s and create_time <= %s{clause}
        group by db_name
        order by sum(effect_row) desc
        limit 20;"""
        return self.__query(sql, [start_date, end_date] + params)

    # 慢日志 db/user 维度统计（v2 明细表，近 24 小时；替代 v1 mysql_slow_query_review_history）
    def slow_query_count_by_db_by_user(self, instance_ids=None):
        from datetime import datetime

        from django.db.models import Count
        from sql.models import MySQLSlowQueryDetail

        if instance_ids is not None and not instance_ids:
            return {"rows": []}
        cutoff = datetime.now() - timedelta(hours=24)
        qs = MySQLSlowQueryDetail.objects.filter(
            execution_start_time__gte=cutoff
        ).exclude(db_name="")
        if instance_ids is not None:
            qs = qs.filter(instance_id__in=instance_ids)
        rows = (
            qs.values("db_name", "user_name")
            .annotate(cnt=Count("id"))
            .order_by("-cnt")[:50]
        )
        return {
            "rows": [(f"{r['db_name']} user: {r['user_name']}", r["cnt"]) for r in rows]
        }

    # 慢日志 db 维度统计（v2 明细表，近 24 小时）
    def slow_query_count_by_db(self, instance_ids=None):
        from datetime import datetime

        from django.db.models import Count
        from sql.models import MySQLSlowQueryDetail

        if instance_ids is not None and not instance_ids:
            return {"rows": []}
        cutoff = datetime.now() - timedelta(hours=24)
        qs = MySQLSlowQueryDetail.objects.filter(
            execution_start_time__gte=cutoff
        ).exclude(db_name="")
        if instance_ids is not None:
            qs = qs.filter(instance_id__in=instance_ids)
        rows = qs.values("db_name").annotate(cnt=Count("id")).order_by("-cnt")[:50]
        return {"rows": [(r["db_name"], r["cnt"]) for r in rows]}

    # 数据库实例类型统计
    def instance_count_by_type(self):
        sql = """
        select db_type,count(1) as cn 
        from sql_instance 
        group by db_type 
        order by 2 desc;"""
        return self.__query(sql)

    def query_sql_prod_bill(self, start_date, end_date, group_ids=None):
        clause, params = self._group_clause(group_ids, alias="a")
        sql = f"""
            SELECT
                CASE
                        a.STATUS 
                        WHEN 'workflow_finish' THEN
                        '已正常结束' 
                        WHEN 'workflow_autoreviewwrong' THEN
                        '自动审核不通过' 
                        WHEN 'workflow_abort' THEN
                        '人工终止流程' 
                        WHEN 'workflow_exception' THEN
                        '执行有异常' 
                        WHEN 'workflow_review_pass' THEN
                        '审核通过' 
                        WHEN 'workflow_queuing' THEN
                        '排队中' 
                        WHEN 'workflow_executing' THEN
                        '确认中' 
                        WHEN 'workflow_manreviewing' THEN
                        '等待审核人审核' ELSE '未知状态' 
                    END AS status_desc,
                    COUNT( 1 ) AS count 
                FROM sql_workflow a
                    INNER JOIN sql_instance b ON ( a.instance_id = b.id ) 
                WHERE a.create_time >= %s and a.create_time <= %s{clause}
                GROUP BY a.STATUS
                ORDER BY 1;
          """
        return self.__query(sql, [start_date, end_date] + params)

    def query_instance_env_info(self):
        sql = """
             SELECT
            db_type,
            type,
            COUNT(1) AS cn
        FROM
            sql_instance
        GROUP BY
            db_type,
            type
        ORDER BY
            1,
            2;
        """
        return self.__query(sql)
