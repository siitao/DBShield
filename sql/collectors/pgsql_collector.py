# -*- coding:utf-8 -*-
"""
PgSQL 慢查询采集器

数据来源：pg_stat_statements 扩展
"""
import logging
from datetime import datetime

from .base import BaseSlowQueryCollector, update_or_create_with_retry

logger = logging.getLogger("default")


class PgSQLSlowQueryCollector(BaseSlowQueryCollector):
    """PgSQL 慢查询采集器"""

    def collect_summary(self, start_time: datetime, end_time: datetime):
        """从 pg_stat_statements 采集统计数据"""
        try:
            engine = self.get_engine()

            # 直接尝试查询 pg_stat_statements，如果失败则说明扩展未安装
            sql = """
                SELECT
                    queryid::text AS sql_hash,
                    query AS fingerprint,
                    calls AS total_execution_counts,
                    total_exec_time / 1000 AS total_execution_times,
                    mean_exec_time / 1000 AS query_time_avg,
                    rows AS rows_sum,
                    CASE WHEN calls > 0
                        THEN rows::float / calls
                        ELSE 0
                    END AS rows_avg,
                    shared_blks_hit,
                    shared_blks_read,
                    NULL::timestamp AS first_seen,
                    now()::timestamp AS last_seen,
                    dbid
                FROM pg_stat_statements
                WHERE mean_exec_time > 1000
                    AND calls > 0
                ORDER BY total_exec_time DESC
                LIMIT 1000
            """
            result = engine.query(sql=sql)

            # 检查是否有错误
            if result.error:
                logger.warning(
                    f"[{self.instance_name}] 查询 pg_stat_statements 失败: {result.error}，"
                    f"请确保在当前数据库中执行了 CREATE EXTENSION pg_stat_statements;"
                )
                return

            if not result.rows:
                self.log_collect_result("统计", 0)
                return

            # 获取数据库名称映射
            db_map = {}
            try:
                db_result = engine.query(
                    sql="SELECT oid, datname FROM pg_database"
                )
                if db_result.rows:
                    db_map = {row[0]: row[1] for row in db_result.rows}
            except Exception:
                pass

            # 保存到数据库
            from sql.models import PgSQLSlowQuerySummary

            created_count = 0
            updated_count = 0
            now = datetime.now()

            for row in result.rows:
                sql_hash = row[0]
                fingerprint = row[1]
                total_execution_counts = row[2] or 0
                total_execution_times = row[3] or 0
                query_time_avg = row[4] or 0
                rows_sum = row[5] or 0
                rows_avg = row[6] or 0
                shared_blks_hit = row[7] or 0
                shared_blks_read = row[8] or 0
                first_seen = row[9]
                last_seen = row[10]
                dbid = row[11]

                db_name = db_map.get(dbid, "")

                # 处理 bytes 类型
                if isinstance(fingerprint, bytes):
                    fingerprint = fingerprint.decode("utf-8", errors="replace")

                # 使用 update_or_create，存在则更新，不存在则创建（带重试与行级隔离）
                try:
                    obj, created = update_or_create_with_retry(
                        PgSQLSlowQuerySummary,
                        lookup={
                            "instance_id": self.instance_id,
                            "sql_hash": sql_hash,
                        },
                        defaults={
                            "fingerprint": fingerprint,
                            "total_execution_counts": total_execution_counts,
                            "total_execution_times": total_execution_times,
                            "query_time_avg": query_time_avg,
                            "rows_sum": rows_sum,
                            "rows_avg": rows_avg,
                            "shared_blks_hit": shared_blks_hit,
                            "shared_blks_read": shared_blks_read,
                            "first_seen": first_seen or now,
                            "last_seen": last_seen or now,
                            "db_name": db_name,
                        },
                    )
                except Exception:
                    logger.warning(
                        f"[{self.instance_name}] 统计单行upsert失败，跳过: "
                        f"sql_hash={sql_hash}",
                        exc_info=True,
                    )
                    continue
                if created:
                    created_count += 1
                else:
                    updated_count += 1

            self.log_collect_result("统计", created_count + updated_count)
            logger.info(f"[{self.instance_name}] PgSQL统计: 新增 {created_count}, 更新 {updated_count}")

        except Exception as e:
            logger.error(f"[{self.instance_name}] 采集PgSQL统计数据失败: {e}", exc_info=True)

    def collect_detail(self, start_time: datetime, end_time: datetime):
        """
        从 pg_stat_statements 采集"伪明细"数据

        注意：pg_stat_statements 只提供聚合数据，此处将每条 SQL 的统计
        作为一条明细记录写入，用于在界面上展示 SQL 列表。
        使用 update_or_create 避免重复记录。
        """
        try:
            engine = self.get_engine()

            # 查询 pg_stat_statements（只查平均执行时间 > 1秒的 SQL）
            sql = """
                SELECT
                    queryid::text AS sql_hash,
                    query AS sql_text,
                    calls AS total_execution_counts,
                    total_exec_time / 1000 AS total_execution_times,
                    mean_exec_time / 1000 AS query_time_avg,
                    rows AS rows_sent,
                    shared_blks_hit,
                    shared_blks_read,
                    dbid
                FROM pg_stat_statements
                WHERE calls > 0
                    AND mean_exec_time > 1000
                ORDER BY total_exec_time DESC
                LIMIT 500
            """
            result = engine.query(sql=sql)

            if result.error or not result.rows:
                self.log_collect_result("明细", 0)
                return

            # 获取数据库名称映射
            db_map = {}
            try:
                db_result = engine.query(sql="SELECT oid, datname FROM pg_database")
                if db_result.rows:
                    db_map = {row[0]: row[1] for row in db_result.rows}
            except Exception:
                pass

            from sql.models import PgSQLSlowQueryDetail

            created_count = 0
            updated_count = 0
            now = datetime.now()

            for row in result.rows:
                sql_hash = row[0]
                sql_text = row[1]
                calls = row[2] or 0
                total_time = row[3] or 0
                mean_time = row[4] or 0
                rows_sent = row[5] or 0
                blks_hit = row[6] or 0
                blks_read = row[7] or 0
                dbid = row[8]

                db_name = db_map.get(dbid, "")

                # 处理 bytes 类型
                if isinstance(sql_text, bytes):
                    sql_text = sql_text.decode("utf-8", errors="replace")

                # 使用 update_or_create 避免重复记录
                # 按 instance_id + sql_hash 去重，同一实例同一 SQL 只保留一条
                # （带死锁/冲突重试与行级隔离）
                try:
                    obj, created = update_or_create_with_retry(
                        PgSQLSlowQueryDetail,
                        lookup={
                            "instance_id": self.instance_id,
                            "sql_hash": sql_hash,
                        },
                        defaults={
                            "execution_start_time": now,
                            "host_address": "",
                            "user_name": "",
                            "db_name": db_name,
                            "sql_text": sql_text,
                            "query_time": mean_time,
                            "rows_sent": rows_sent // calls if calls > 0 else 0,
                            "shared_blks_hit": blks_hit // calls if calls > 0 else 0,
                            "shared_blks_read": blks_read // calls if calls > 0 else 0,
                        },
                    )
                except Exception:
                    logger.warning(
                        f"[{self.instance_name}] 明细单行upsert失败，跳过: "
                        f"sql_hash={sql_hash}",
                        exc_info=True,
                    )
                    continue
                if created:
                    created_count += 1
                else:
                    updated_count += 1

            self.log_collect_result("明细", created_count + updated_count)
            logger.info(f"[{self.instance_name}] PgSQL明细: 新增 {created_count}, 更新 {updated_count}")

        except Exception as e:
            logger.error(f"[{self.instance_name}] 采集PgSQL明细数据失败: {e}", exc_info=True)
