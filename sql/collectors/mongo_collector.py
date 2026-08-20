# -*- coding:utf-8 -*-
"""
MongoDB 慢查询采集器

数据来源：Database Profiler (system.profile 集合)
"""
import hashlib
import json
import logging
from datetime import datetime, timezone

from .base import BaseSlowQueryCollector, CursorManager

logger = logging.getLogger("default")


class MongoSlowQueryCollector(BaseSlowQueryCollector):
    """MongoDB 慢查询采集器"""

    @staticmethod
    def _local_naive_to_utc(local_naive: datetime) -> datetime:
        """本地 naive 时间 → UTC aware 时间。

        Mongo system.profile 的 ts 是 UTC，而游标/任务侧时间统一为
        本地 naive（settings.TIME_ZONE）。pymongo 会把 naive 编码为
        UTC、aware 按时区换算，此前直接用本地 naive 游标查询导致
        每轮漏采（时区差）数据，必须显式换算后再比较（H5，2026-08-20 审查）。
        """
        from django.utils import timezone as dj_timezone

        if local_naive is None:
            return local_naive
        return (
            dj_timezone.make_aware(local_naive, dj_timezone.get_current_timezone())
            .astimezone(timezone.utc)
        )

    @staticmethod
    def _utc_naive_to_local_naive(utc_naive: datetime) -> datetime:
        """Mongo 返回的 UTC naive 时间 → 本地 naive 时间（入库/游标存储语义）"""
        from django.utils import timezone as dj_timezone

        if utc_naive is None:
            return utc_naive
        return (
            dj_timezone.make_aware(utc_naive, timezone.utc)
            .astimezone(dj_timezone.get_current_timezone())
            .replace(tzinfo=None)
        )

    def _get_mongo_client(self):
        """获取 MongoDB 连接"""
        import pymongo

        host = self.instance.host
        port = self.instance.port
        user = self.instance.user
        password = self.instance.password

        if user and password:
            uri = f"mongodb://{user}:{password}@{host}:{port}/"
        else:
            uri = f"mongodb://{host}:{port}/"

        return pymongo.MongoClient(uri, serverSelectionTimeoutMS=5000)

    def _serialize_mongo_value(self, obj):
        """处理 MongoDB 特殊类型的序列化"""
        import uuid
        from bson import ObjectId

        if isinstance(obj, bytes):
            # bytes 转换为十六进制字符串
            try:
                # 尝试作为 UUID 解析
                if len(obj) == 16:
                    return str(uuid.UUID(bytes=obj))
            except Exception:
                pass
            return obj.hex()
        elif isinstance(obj, ObjectId):
            return str(obj)
        elif isinstance(obj, uuid.UUID):
            return str(obj)
        elif isinstance(obj, datetime):
            return obj.isoformat()
        return str(obj)

    def _normalize_command(self, command: dict) -> str:
        """标准化 MongoDB 命令为指纹"""
        try:
            # 移除值，保留结构
            normalized = self._remove_values(command)
            return json.dumps(normalized, sort_keys=True, default=self._serialize_mongo_value)
        except Exception:
            return str(command)

    def _remove_values(self, obj):
        """递归移除值，保留结构"""
        if isinstance(obj, dict):
            return {k: self._remove_values(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._remove_values(item) for item in obj[:1]]  # 只保留第一个元素结构
        elif isinstance(obj, str):
            return "?"
        elif isinstance(obj, (int, float)):
            return 0
        elif isinstance(obj, bool):
            return False
        else:
            return "?"

    def collect_summary(self, start_time: datetime, end_time: datetime):
        """从 system.profile 聚合统计数据"""
        try:
            client = self._get_mongo_client()

            # 获取数据库列表
            db_names = client.list_database_names()
            if not db_names:
                self.log_collect_result("统计", 0)
                return

            from sql.models import MongoSlowQuerySummary

            created_count = 0

            for db_name in db_names:
                try:
                    db = client[db_name]

                    # 检查 profiler 状态
                    try:
                        profiler_level = db.command("profile", -1)
                        if profiler_level.get("was", 0) == 0:
                            continue  # profiler 未开启
                    except Exception:
                        continue

                    # 聚合查询（start/end 为本地 naive，须换算为 UTC aware 再与 ts 比较）
                    pipeline = [
                        {
                            "$match": {
                                "ts": {
                                    "$gte": self._local_naive_to_utc(start_time),
                                    "$lte": self._local_naive_to_utc(end_time),
                                },
                                "op": {"$ne": "command"},  # 排除 profiler 命令本身
                            }
                        },
                        {
                            "$group": {
                                "_id": {
                                    "ns": "$ns",
                                    "op": "$op",
                                },
                                "count": {"$sum": 1},
                                "total_duration": {"$sum": "$millis"},
                                "avg_duration": {"$avg": "$millis"},
                                "docs_examined_avg": {"$avg": "$docsExamined"},
                                "docs_returned_avg": {"$avg": "$nreturned"},
                                "has_sort": {
                                    "$max": {"$cond": [{"$ifNull": ["$hasSortStage", False]}, 1, 0]}
                                },
                                "first_seen": {"$min": "$ts"},
                                "last_seen": {"$max": "$ts"},
                                "sample": {"$first": "$command"},
                                "sample_ns": {"$first": "$ns"},
                            }
                        },
                        {"$sort": {"total_duration": -1}},
                        {"$limit": 1000},
                    ]

                    results = list(db.system.profile.aggregate(pipeline))

                    for doc in results:
                        ns = doc.get("sample_ns", doc["_id"].get("ns", ""))
                        op = doc["_id"].get("op", "unknown")
                        sample = doc.get("sample", {})
                        first_seen = doc.get("first_seen")
                        last_seen = doc.get("last_seen")

                        # MongoDB 的时间是 UTC，转换为本地时间（入库/游标统一本地 naive）
                        if first_seen and first_seen.tzinfo is None:
                            first_seen = self._utc_naive_to_local_naive(first_seen)
                        if last_seen and last_seen.tzinfo is None:
                            last_seen = self._utc_naive_to_local_naive(last_seen)

                        # 解析集合名
                        collection_name = ns.split(".")[-1] if "." in ns else ns

                        # 生成指纹
                        fingerprint = self._normalize_command(sample)
                        sql_hash = hashlib.md5(fingerprint.encode("utf-8")).hexdigest()

                        # 获取示例命令文本（处理特殊类型）
                        sample_sql = json.dumps(sample, default=self._serialize_mongo_value, ensure_ascii=False)[:2000]

                        _, created = MongoSlowQuerySummary.objects.update_or_create(
                            instance_id=self.instance_id,
                            sql_hash=sql_hash,
                            defaults={
                                "fingerprint": fingerprint[:2000],
                                "sample_sql": sample_sql,
                                "db_name": db_name,
                                "collection_name": collection_name,
                                "operation_type": op,
                                "total_execution_counts": doc.get("count", 0),
                                "total_execution_times": doc.get("total_duration", 0),
                                "query_time_avg": doc.get("avg_duration", 0),
                                "query_time_p95": 0,  # 需要额外计算
                                "docs_examined_avg": doc.get("docs_examined_avg", 0),
                                "docs_returned_avg": doc.get("docs_returned_avg", 0),
                                "has_sort": bool(doc.get("has_sort", 0)),
                                "first_seen": first_seen,
                                "last_seen": last_seen,
                            },
                        )
                        if created:
                            created_count += 1

                except Exception as e:
                    logger.warning(f"[{self.instance_name}] 采集数据库 {db_name} 统计失败: {e}")
                    continue

            client.close()
            self.log_collect_result("统计", created_count)

        except Exception as e:
            logger.error(f"[{self.instance_name}] 采集MongoDB统计数据失败: {e}", exc_info=True)

    def collect_detail(self, start_time: datetime, end_time: datetime):
        """从 system.profile 采集明细数据（使用统一游标管理器）"""
        try:
            client = self._get_mongo_client()

            # 创建游标管理器
            cursor_mgr = self.create_cursor_manager()

            from sql.models import MongoSlowQueryDetail

            detail_list = []

            db_names = client.list_database_names()

            for db_name in db_names:
                try:
                    db = client[db_name]

                    # 检查 profiler 状态
                    try:
                        profiler_level = db.command("profile", -1)
                        if profiler_level.get("was", 0) == 0:
                            continue
                    except Exception:
                        continue

                    # 查询慢操作（使用游标管理器的 cursor 属性；
                    # 游标与 end_time 均为本地 naive，统一换算为 UTC aware 再比较）
                    query = {
                        "ts": {
                            "$gt": self._local_naive_to_utc(cursor_mgr.cursor),
                            "$lte": self._local_naive_to_utc(end_time),
                        },
                        "millis": {"$gte": 100},  # 慢查询阈值
                        "op": {"$ne": "command"},
                    }

                    docs = list(
                        db.system.profile.find(query).sort("ts", 1).limit(10000)
                    )

                    for doc in docs:
                        ns = doc.get("ns", "")
                        op = doc.get("op", "unknown")
                        command = doc.get("command", {})
                        ts = doc.get("ts")

                        # MongoDB 的 ts 是 UTC 时间，转换为本地时间（入库/游标统一本地 naive）
                        if ts and ts.tzinfo is None:
                            ts = self._utc_naive_to_local_naive(ts)

                        # 使用游标管理器判断是否是新数据
                        if not cursor_mgr.is_new_data(ts):
                            continue

                        # 解析集合名
                        collection_name = ns.split(".")[-1] if "." in ns else ns

                        # 生成指纹
                        fingerprint = self._normalize_command(command)
                        sql_hash = hashlib.md5(fingerprint.encode("utf-8")).hexdigest()

                        # 获取命令文本（处理特殊类型）
                        command_text = json.dumps(command, default=self._serialize_mongo_value, ensure_ascii=False)[:2000]

                        # 客户端信息
                        client_info = doc.get("client", "")
                        user_name = doc.get("user", "")

                        # 添加到批量插入列表
                        detail_list.append(MongoSlowQueryDetail(
                            instance_id=self.instance_id,
                            sql_hash=sql_hash,
                            operation_type=op,
                            execution_start_time=ts,
                            host_address=client_info,
                            user_name=user_name,
                            db_name=db_name,
                            collection_name=collection_name,
                            command_text=command_text,
                            duration=doc.get("millis", 0),
                            docs_examined=doc.get("docsExamined", 0),
                            docs_returned=doc.get("nreturned", 0),
                            nreturned=doc.get("nreturned", 0),
                            has_sort=bool(doc.get("hasSortStage", False)),
                            plan_summary=str(doc.get("planSummary", "")),
                        ))

                        # 更新游标管理器
                        cursor_mgr.update_max_cursor(ts)

                except Exception as e:
                    logger.warning(f"[{self.instance_name}] 采集数据库 {db_name} 明细失败: {e}")
                    continue

            # 批量插入（每批500条）
            if detail_list:
                MongoSlowQueryDetail.objects.bulk_create(detail_list, batch_size=500)
                logger.info(f"[{self.instance_name}] 批量插入 {len(detail_list)} 条 MongoDB 慢日志")

            # 提交游标更新（统一逻辑）
            cursor_mgr.commit()

            client.close()
            self.log_collect_result("明细", len(detail_list))

        except Exception as e:
            logger.error(f"[{self.instance_name}] 采集MongoDB明细数据失败: {e}", exc_info=True)
