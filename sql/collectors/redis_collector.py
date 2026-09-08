# -*- coding:utf-8 -*-
"""
Redis 慢查询采集器

数据来源：SLOWLOG GET 命令
"""
import logging
from datetime import datetime

from .base import BaseSlowQueryCollector, CursorManager, update_or_create_with_retry

logger = logging.getLogger("default")


class RedisSlowQueryCollector(BaseSlowQueryCollector):
    """Redis 慢查询采集器"""

    def _get_redis_connection(self):
        """获取 Redis 连接"""
        import redis

        return redis.Redis(
            host=self.instance.host,
            port=self.instance.port,
            password=self.instance.password or None,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
        )

    def _normalize_command(self, command_parts: list) -> str:
        """
        标准化 Redis 命令为指纹

        例如：SET key1 value1 -> SET key1 *
        """
        if not command_parts:
            return ""

        parts = [str(p) for p in command_parts]
        cmd = parts[0].upper()

        # 简单命令直接返回
        if len(parts) <= 1:
            return cmd

        # 保留命令名和第一个参数（通常是key），其余替换为 *
        normalized_parts = [cmd, parts[1]]
        if len(parts) > 2:
            normalized_parts.append("*")

        return " ".join(normalized_parts)

    def collect_summary(self, start_time: datetime, end_time: datetime):
        """采集统计数据（从明细聚合）"""
        try:
            from sql.models import RedisSlowQuerySummary, RedisSlowQueryDetail

            # 从明细数据聚合统计
            details = RedisSlowQueryDetail.objects.filter(
                instance_id=self.instance_id,
                execution_start_time__gte=start_time,
                execution_start_time__lte=end_time,
            )

            if not details.exists():
                self.log_collect_result("统计", 0)
                return

            # 按 sql_hash 聚合
            from django.db.models import Count, Sum, Avg, Min, Max

            stats = details.values("sql_hash").annotate(
                total_count=Count("id"),
                total_duration=Sum("duration"),
                avg_duration=Avg("duration"),
                min_duration=Min("duration"),
                max_duration=Max("duration"),
                first_seen=Min("execution_start_time"),
                last_seen=Max("execution_start_time"),
            )

            created_count = 0
            for stat in stats:
                # 获取示例命令
                sample_detail = details.filter(sql_hash=stat["sql_hash"]).first()
                sample_sql = sample_detail.command_text if sample_detail else ""
                fingerprint = self._normalize_command(sample_sql.split()) if sample_sql else ""

                # 带死锁/冲突重试与行级隔离
                try:
                    _, created = update_or_create_with_retry(
                        RedisSlowQuerySummary,
                        lookup={
                            "instance_id": self.instance_id,
                            "sql_hash": stat["sql_hash"],
                        },
                        defaults={
                            "fingerprint": fingerprint,
                            "sample_sql": sample_sql,
                            "total_execution_counts": stat["total_count"],
                            "total_execution_times": stat["total_duration"] or 0,
                            "query_time_avg": stat["avg_duration"] or 0,
                            "query_time_p95": 0,  # 需要额外计算
                            "first_seen": stat["first_seen"],
                            "last_seen": stat["last_seen"],
                        },
                    )
                except Exception:
                    logger.warning(
                        f"[{self.instance_name}] 统计单行upsert失败，跳过: "
                        f"sql_hash={stat['sql_hash']}",
                        exc_info=True,
                    )
                    continue
                if created:
                    created_count += 1

            self.log_collect_result("统计", created_count)

        except Exception as e:
            logger.error(f"[{self.instance_name}] 采集Redis统计数据失败: {e}", exc_info=True)

    def collect_detail(self, start_time: datetime, end_time: datetime):
        """从 SLOWLOG GET 采集明细数据（使用统一游标管理器）"""
        try:
            r = self._get_redis_connection()

            # 创建游标管理器
            cursor_mgr = self.create_cursor_manager()

            # 获取慢日志
            slowlog = r.slowlog_get(1000)

            if not slowlog:
                self.log_collect_result("明细", 0)
                return

            from sql.models import RedisSlowQueryDetail

            detail_list = []

            for entry in slowlog:
                # redis-py 5.x 返回字典格式
                entry_id = entry.get("id", 0)
                start_time_ts = entry.get("start_time", 0)
                duration = entry.get("duration", 0)  # 微秒
                command_parts = entry.get("command", [])
                # redis-py 5.x 字段名是 client_address，3.x 是 client
                client_info = entry.get("client_address", "") or entry.get("client", "")

                # 处理 bytes 类型
                if isinstance(client_info, bytes):
                    client_info = client_info.decode("utf-8", errors="replace")

                # command 可能是 bytes 类型，需要解码
                if isinstance(command_parts, bytes):
                    command_parts = command_parts.decode("utf-8", errors="replace").split()
                elif isinstance(command_parts, (list, tuple)):
                    # 将 bytes 元素转为字符串
                    command_parts = [
                        p.decode("utf-8", errors="replace") if isinstance(p, bytes) else str(p)
                        for p in command_parts
                    ]

                # 如果 client 为空，使用实例地址作为默认值
                if not client_info:
                    client_info = f"{self.instance.host}:{self.instance.port}"

                # 转换时间戳
                exec_time = datetime.fromtimestamp(start_time_ts)

                # 使用游标管理器判断是否是新数据
                if not cursor_mgr.is_new_data(exec_time):
                    continue

                # 标准化命令
                if isinstance(command_parts, (list, tuple)):
                    command_text = " ".join(str(p) for p in command_parts)
                    fingerprint = self._normalize_command(command_parts)
                else:
                    command_text = str(command_parts)
                    fingerprint = command_text

                # 生成 hash
                sql_hash = self.generate_hash(fingerprint)

                # 添加到批量插入列表
                detail_list.append(RedisSlowQueryDetail(
                    instance_id=self.instance_id,
                    sql_hash=sql_hash,
                    execution_start_time=exec_time,
                    host_address=client_info,
                    command_text=command_text,
                    duration=duration,
                ))

                # 更新游标管理器
                cursor_mgr.update_max_cursor(exec_time)

            # 批量插入（每批500条）
            if detail_list:
                RedisSlowQueryDetail.objects.bulk_create(detail_list, batch_size=500)
                logger.info(f"[{self.instance_name}] 批量插入 {len(detail_list)} 条 Redis 慢日志")

            # 提交游标更新（统一逻辑：只有有新数据时才更新）
            cursor_mgr.commit()

            self.log_collect_result("明细", len(detail_list))

        except Exception as e:
            logger.error(f"[{self.instance_name}] 采集Redis明细数据失败: {e}", exc_info=True)
