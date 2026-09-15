# -*- coding: UTF-8 -*-

from datetime import timedelta
from django.db import connection


class ChartDao(object):
    # 直接在DBShield数据库查询数据，用于报表
    @staticmethod
    def __query(sql):
        cursor = connection.cursor()
        cursor.execute(sql)
        rows = cursor.fetchall()
        fields = cursor.description
        column_list = []
        if fields:
            for i in fields:
                column_list.append(i[0])
        return {"column_list": column_list, "rows": rows}

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
    def syntax_type(self, start_date, end_date):
        sql = """
        select
          case when syntax_type = 1
            then 'DDL'
          when syntax_type = 2
            then 'DML'
          else '其他'
          end as syntax_type,
          count(*)
        from sql_workflow 
        where create_time >= '{}' and create_time <= '{}'
        group by syntax_type;""".format(start_date, end_date)
        return self.__query(sql)

    # 工单数量统计
    def workflow_by_date(self, start_date, end_date):
        sql = """
        select
          date_format(create_time, '%Y-%m-%d'),
          count(*)
        from sql_workflow
        where create_time >= '{}' and create_time <= '{}'
        group by date_format(create_time, '%Y-%m-%d')
        order by 1 asc;""".format(start_date, end_date)
        return self.__query(sql)

    # 工单按组统计
    def workflow_by_group(self, start_date, end_date):
        sql = """
        select
          group_name,
          count(*)
        from sql_workflow
        where create_time >= '{}' and create_time <= '{}'
        group by group_id
        order by count(*) desc;""".format(start_date, end_date)
        return self.__query(sql)

    def workflow_by_user(self, start_date, end_date):
        """工单按人统计"""
        # TODO select 的对象应该为engineer ID, 查询时应作联合查询查出用户中文名
        sql = """
        select
          engineer_display,
          count(*)
        from sql_workflow
        where create_time >= '{}' and create_time <= '{}'
        group by engineer_display
        order by count(*) desc;""".format(start_date, end_date)
        return self.__query(sql)

    # SQL查询统计(每日检索行数)
    def querylog_effect_row_by_date(self, start_date, end_date):
        sql = """
        select
          date_format(create_time, '%Y-%m-%d'),
          sum(effect_row)
        from query_log
        where create_time >= '{}' and create_time <= '{}'
        group by date_format(create_time, '%Y-%m-%d')
        order by sum(effect_row) desc;""".format(start_date, end_date)
        return self.__query(sql)

    # SQL查询统计(每日检索次数)
    def querylog_count_by_date(self, start_date, end_date):
        sql = """
        select
          date_format(create_time, '%Y-%m-%d'),
          count(*)
        from query_log
        where create_time >= '{}' and create_time <= '{}'
        group by date_format(create_time, '%Y-%m-%d')
        order by count(*) desc;""".format(start_date, end_date)
        return self.__query(sql)

    # SQL查询统计(用户检索行数)
    def querylog_effect_row_by_user(self, start_date, end_date):
        sql = """
        select 
          user_display,
          sum(effect_row)
        from query_log
        where create_time >= '{}' and create_time <= '{}'
        group by user_display
        order by sum(effect_row) desc
        limit 20;""".format(start_date, end_date)
        return self.__query(sql)

    # SQL查询统计(DB检索行数)
    def querylog_effect_row_by_db(self, start_date, end_date):
        sql = """
       select
          db_name,
          sum(effect_row)
        from query_log
        where create_time >= '{}' and create_time <= '{}'
        group by db_name
        order by sum(effect_row) desc
        limit 20;""".format(start_date, end_date)
        return self.__query(sql)

    # 慢日志 db/user 维度统计（v2 明细表，近 24 小时；替代 v1 mysql_slow_query_review_history）
    def slow_query_count_by_db_by_user(self):
        from datetime import datetime

        from django.db.models import Count
        from sql.models import MySQLSlowQueryDetail

        cutoff = datetime.now() - timedelta(hours=24)
        rows = (
            MySQLSlowQueryDetail.objects
            .filter(execution_start_time__gte=cutoff)
            .exclude(db_name="")
            .values("db_name", "user_name")
            .annotate(cnt=Count("id"))
            .order_by("-cnt")[:50]
        )
        return {
            "rows": [
                (f"{r['db_name']} user: {r['user_name']}", r["cnt"])
                for r in rows
            ]
        }

    # 慢日志 db 维度统计（v2 明细表，近 24 小时）
    def slow_query_count_by_db(self):
        from datetime import datetime

        from django.db.models import Count
        from sql.models import MySQLSlowQueryDetail

        cutoff = datetime.now() - timedelta(hours=24)
        rows = (
            MySQLSlowQueryDetail.objects
            .filter(execution_start_time__gte=cutoff)
            .exclude(db_name="")
            .values("db_name")
            .annotate(cnt=Count("id"))
            .order_by("-cnt")[:50]
        )
        return {"rows": [(r["db_name"], r["cnt"]) for r in rows]}

    # 数据库实例类型统计
    def instance_count_by_type(self):
        sql = """
        select db_type,count(1) as cn 
        from sql_instance 
        group by db_type 
        order by 2 desc;"""
        return self.__query(sql)

    def query_sql_prod_bill(self, start_date, end_date):
        sql = """
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
                WHERE a.create_time >= '{}' and a.create_time <= '{}'
                GROUP BY a.STATUS
                ORDER BY 1;
          """.format(start_date, end_date)
        return self.__query(sql)

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
