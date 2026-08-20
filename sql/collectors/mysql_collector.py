# -*- coding:utf-8 -*-
"""
MySQL 慢查询采集器

数据来源：
- 统计数据：performance_schema.events_statements_summary_by_digest
- 明细数据：mysql.slow_log
"""
import logging
from datetime import datetime

from django.utils import timezone

from .base import BaseSlowQueryCollector, CursorManager

logger = logging.getLogger("default")


class MySQLSlowQueryCollector(BaseSlowQueryCollector):
    """MySQL 慢查询采集器"""

    # 明细表 sql_text 列上限（managed=False，真实列类型由部署侧建表脚本决定，
    # 常见为 varchar(2048)）。超长 SQL 原样入库会触发 DataError 1406 崩掉整批
    SQL_TEXT_MAX = 2048
    # 每片入库行数：片内单个事务，commit 小、被超时打断只丢当前片
    INSERT_CHUNK_SIZE = 200

    def _insert_detail_rows(self, rows):
        """分片批量入库，失败时逐条降级，单条仍失败则跳过并记日志。

        保证：单条超长/非法数据不能拖垮整批、更不能让游标无法提交
        （否则同一批数据每 5 分钟重扫，形成死循环且明细全部丢失）；
        分片提交避免单个超大事务在慢库上 commit 超时（TimeoutException）。
        """
        from sql.models import MySQLSlowQueryDetail

        saved = 0
        for chunk_start in range(0, len(rows), self.INSERT_CHUNK_SIZE):
            chunk = rows[chunk_start : chunk_start + self.INSERT_CHUNK_SIZE]
            try:
                MySQLSlowQueryDetail.objects.bulk_create(chunk, batch_size=500)
                saved += len(chunk)
                continue
            except Exception as chunk_e:
                logger.warning(
                    f"[{self.instance_name}] 明细分片({len(chunk)}条)入库失败，降级逐条插入: {chunk_e}"
                )
            for r in chunk:
                r.sql_text = (r.sql_text or "")[: self.SQL_TEXT_MAX]
                try:
                    MySQLSlowQueryDetail.objects.create(
                        instance_id=r.instance_id,
                        sql_hash=r.sql_hash,
                        execution_start_time=r.execution_start_time,
                        host_address=r.host_address,
                        user_name=r.user_name,
                        db_name=r.db_name,
                        sql_text=r.sql_text,
                        query_time=r.query_time,
                        lock_time=r.lock_time,
                        rows_sent=r.rows_sent,
                        rows_examined=r.rows_examined,
                    )
                    saved += 1
                except Exception as row_e:
                    logger.warning(
                        f"[{self.instance_name}] 跳过无法入库的慢查询明细: {row_e}"
                    )
        if saved != len(rows):
            logger.warning(
                f"[{self.instance_name}] 明细入库 {saved}/{len(rows)} 条，"
                f"其余因列超长/非法数据被跳过"
            )
        return saved

    def collect_summary(self, start_time: datetime, end_time: datetime):
        """
        从 performance_schema 采集统计数据

        注意：performance_schema 的时间字段是相对值，需要特殊处理
        """
        try:
            engine = self.get_engine()

            # 检查 performance_schema 是否开启
            check_sql = "SHOW VARIABLES LIKE 'performance_schema'"
            result = engine.query(sql=check_sql)
            if not result.rows or result.rows[0][1] != "ON":
                logger.warning(f"[{self.instance_name}] performance_schema 未开启，跳过统计采集")
                return

            # 从 performance_schema 获取按指纹聚合的统计
            sql = """
                SELECT
                    DIGEST AS sql_hash,
                    DIGEST_TEXT AS fingerprint,
                    COUNT_STAR AS total_execution_counts,
                    SUM_TIMER_WAIT / 1000000000 AS total_execution_times,
                    AVG_TIMER_WAIT / 1000000000 AS query_time_avg,
                    SUM_ROWS_EXAMINED AS parse_total_row_counts,
                    SUM_ROWS_SENT AS return_total_row_counts,
                    CASE WHEN COUNT_STAR > 0
                        THEN SUM_ROWS_EXAMINED / COUNT_STAR
                        ELSE 0
                    END AS parse_row_avg,
                    CASE WHEN COUNT_STAR > 0
                        THEN SUM_ROWS_SENT / COUNT_STAR
                        ELSE 0
                    END AS return_row_avg,
                    FIRST_SEEN AS first_seen,
                    LAST_SEEN AS last_seen,
                    SCHEMA_NAME AS db_name
                FROM performance_schema.events_statements_summary_by_digest
                WHERE SCHEMA_NAME IS NOT NULL
                    AND LAST_SEEN >= %s
                ORDER BY SUM_TIMER_WAIT DESC
                LIMIT 1000
            """
            # 转换时间为字符串格式
            start_str = start_time.strftime("%Y-%m-%d %H:%M:%S")
            result = engine.query(sql=sql, parameters=[start_str])

            if not result.rows:
                self.log_collect_result("统计", 0)
                return

            # 批量保存到数据库
            from sql.models import MySQLSlowQuerySummary

            summary_list = []
            for row in result.rows:
                summary_list.append(
                    MySQLSlowQuerySummary(
                        instance_id=self.instance_id,
                        sql_hash=row[0],
                        fingerprint=row[1],
                        total_execution_counts=row[2] or 0,
                        total_execution_times=row[3] or 0,
                        query_time_avg=row[4] or 0,
                        query_time_p95=0,  # 需要历史数据计算
                        parse_total_row_counts=row[5] or 0,
                        return_total_row_counts=row[6] or 0,
                        parse_row_avg=row[7] or 0,
                        return_row_avg=row[8] or 0,
                        first_seen=row[9],
                        last_seen=row[10],
                        db_name=row[11],
                    )
                )

            # 使用 update_or_create 处理重复数据
            created_count = 0
            for summary in summary_list:
                _, created = MySQLSlowQuerySummary.objects.update_or_create(
                    instance_id=self.instance_id,
                    sql_hash=summary.sql_hash,
                    defaults={
                        "fingerprint": summary.fingerprint,
                        "total_execution_counts": summary.total_execution_counts,
                        "total_execution_times": summary.total_execution_times,
                        "query_time_avg": summary.query_time_avg,
                        "parse_total_row_counts": summary.parse_total_row_counts,
                        "return_total_row_counts": summary.return_total_row_counts,
                        "parse_row_avg": summary.parse_row_avg,
                        "return_row_avg": summary.return_row_avg,
                        "first_seen": summary.first_seen,
                        "last_seen": summary.last_seen,
                        "db_name": summary.db_name,
                    },
                )
                if created:
                    created_count += 1

            self.log_collect_result("统计", created_count)

        except Exception as e:
            logger.error(f"[{self.instance_name}] 采集MySQL统计数据失败: {e}", exc_info=True)

    def collect_detail(self, start_time: datetime, end_time: datetime):
        """从 mysql.slow_log 采集明细数据（使用统一游标管理器）"""
        try:
            engine = self.get_engine()

            # 检查 slow_log 是否开启
            check_sql = "SHOW VARIABLES LIKE 'slow_query_log'"
            result = engine.query(sql=check_sql)
            if not result.rows or result.rows[0][1] != "ON":
                logger.warning(f"[{self.instance_name}] slow_query_log 未开启，跳过明细采集")
                return

            # 检查 log_output 是否为 TABLE
            check_output = "SHOW VARIABLES LIKE 'log_output'"
            result = engine.query(sql=check_output)
            if not result.rows or result.rows[0][1] != "TABLE":
                logger.warning(f"[{self.instance_name}] log_output 不是 TABLE，跳过明细采集")
                return

            # 创建游标管理器
            cursor_mgr = self.create_cursor_manager()

            # 准备查询参数
            cursor_str = cursor_mgr.cursor.strftime("%Y-%m-%d %H:%M:%S")
            end_str = end_time.strftime("%Y-%m-%d %H:%M:%S")

            # 查询 mysql.slow_log（分批采集，每批1000条）
            batch_size = 1000
            total_count = 0

            while True:
                sql = """
                    SELECT
                        start_time,
                        user_host,
                        db,
                        sql_text,
                        TIME_TO_SEC(query_time) AS query_time,
                        TIME_TO_SEC(lock_time) AS lock_time,
                        rows_sent,
                        rows_examined
                    FROM mysql.slow_log
                    WHERE start_time > %s
                        AND start_time <= %s
                    ORDER BY start_time ASC
                    LIMIT %s
                """
                result = engine.query(sql=sql, parameters=[cursor_str, end_str, batch_size])

                if not result.rows:
                    break

                # 批量处理数据
                from sql.models import MySQLSlowQueryDetail
                detail_list = []

                for row in result.rows:
                    execution_time = row[0]
                    user_host = row[1] or ""
                    db_name = row[2]
                    sql_text = row[3]
                    query_time = row[4] or 0
                    lock_time = row[5] or 0
                    rows_sent = row[6] or 0
                    rows_examined = row[7] or 0

                    # 处理 bytes 类型
                    if isinstance(user_host, bytes):
                        user_host = user_host.decode("utf-8", errors="replace")
                    if isinstance(db_name, bytes):
                        db_name = db_name.decode("utf-8", errors="replace")
                    if isinstance(sql_text, bytes):
                        sql_text = sql_text.decode("utf-8", errors="replace")

                    # 截断超长 SQL：目标明细表列可能为 varchar(2048)，
                    # 原样入库会 DataError 1406 崩掉整批（2026-08-20 线上故障）
                    if sql_text:
                        sql_text = sql_text[: self.SQL_TEXT_MAX]

                    # 计算 sql_hash
                    sql_hash = self.generate_hash(sql_text) if sql_text else ""

                    # 解析 user_host 字段
                    user_name = ""
                    client_ip = ""
                    if user_host:
                        if "[" in user_host:
                            user_name = user_host.split("[")[0].strip()
                        else:
                            user_name = user_host.strip()

                        if "@" in user_host:
                            client_part = user_host.split("@", 1)[1].strip()
                            if "[" in client_part and "]" in client_part:
                                client_ip = client_part.split("[")[1].split("]")[0]
                            else:
                                client_ip = client_part
                        else:
                            if "[" in user_host and "]" in user_host:
                                client_ip = user_host.split("[")[1].split("]")[0]

                    host_address = f"{user_name}@{client_ip}" if client_ip else user_name

                    detail_list.append(MySQLSlowQueryDetail(
                        instance_id=self.instance_id,
                        sql_hash=sql_hash,
                        execution_start_time=execution_time,
                        host_address=host_address,
                        user_name=user_name,
                        db_name=db_name,
                        sql_text=sql_text,
                        query_time=query_time,
                        lock_time=lock_time,
                        rows_sent=rows_sent,
                        rows_examined=rows_examined,
                    ))

                    # 更新游标管理器
                    if execution_time:
                        cursor_mgr.update_max_cursor(execution_time)

                # 批量插入（每批1000条；批量失败自动降级逐条，防单条超长拖垮全批）
                if detail_list:
                    total_count += self._insert_detail_rows(detail_list)

                # 如果本批数据少于 batch_size，说明已采集完毕
                if len(result.rows) < batch_size:
                    break

                # 更新游标字符串继续下一批
                cursor_str = cursor_mgr.max_cursor.strftime("%Y-%m-%d %H:%M:%S")

            # 提交游标更新（统一逻辑）
            cursor_mgr.commit()

            self.log_collect_result("明细", total_count)

        except Exception as e:
            logger.error(f"[{self.instance_name}] 采集MySQL明细数据失败: {e}", exc_info=True)
