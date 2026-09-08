# -*- coding:utf-8 -*-
"""
慢查询采集器基类

统一游标管理逻辑：
- 所有采集器使用 datetime 类型进行游标比较
- 使用 > 比较判断增量数据（不包含游标时间点）
- 只有在有新数据时才更新游标
"""
import hashlib
import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import List, Tuple

from django.db import IntegrityError, OperationalError

logger = logging.getLogger("default")

# 并发 upsert 同一唯一键时，后到事务先撞 1062，Django 按 lock=True 重试
# SELECT ... FOR UPDATE 又可能与对方未提交的插入锁成环报 1213；
# 对方提交后重试即走 UPDATE 路径，故对 1062/1213 做有界重试
UPSERT_MAX_RETRIES = 3
UPSERT_RETRY_BASE_SLEEP = 0.2

# 各慢查询统计表的唯一键（upsert 冲突目标，ON CONFLICT 用字段名）
SUMMARY_UNIQUE_FIELDS = ["instance", "sql_hash"]


def update_or_create_with_retry(model, lookup, defaults):
    """带死锁/唯一键冲突重试的行级 upsert，返回 (obj, created)"""
    for attempt in range(1, UPSERT_MAX_RETRIES + 1):
        try:
            return model.objects.update_or_create(**lookup, defaults=defaults)
        except (IntegrityError, OperationalError):
            if attempt == UPSERT_MAX_RETRIES:
                raise
            time.sleep(UPSERT_RETRY_BASE_SLEEP * attempt)


def bulk_upsert(model, objs, update_fields, batch_size=500, log_label=""):
    """批量 upsert 慢查询统计表（唯一键 instance_id + sql_hash），返回成功落库行数

    MySQL 走 INSERT ... ON DUPLICATE KEY UPDATE（Django 不允许也不需要
    conflict target）；支持 conflict target 的后端（如 PostgreSQL）走
    ON CONFLICT，必须提供 SUMMARY_UNIQUE_FIELDS。
    批量语句失败（如与并发写同键的事务死锁）时降级为逐行带重试 upsert，
    单行失败仅跳过不阻断，其余行继续落库。
    """
    from django.db import connection

    kwargs = {
        "update_conflicts": True,
        "update_fields": update_fields,
        "batch_size": batch_size,
    }
    if connection.features.supports_update_conflicts_with_target:
        kwargs["unique_fields"] = SUMMARY_UNIQUE_FIELDS

    try:
        model.objects.bulk_create(objs, **kwargs)
        return len(objs)
    except (IntegrityError, OperationalError) as e:
        logger.warning(f"{log_label}批量upsert失败，降级逐行重试: {e}")

    persisted = 0
    for obj in objs:
        lookup = {"instance_id": obj.instance_id, "sql_hash": obj.sql_hash}
        defaults = {field: getattr(obj, field) for field in update_fields}
        try:
            update_or_create_with_retry(model, lookup, defaults)
            persisted += 1
        except Exception:
            logger.warning(f"{log_label}单行upsert失败，跳过: {lookup}", exc_info=True)
    return persisted


class CursorManager:
    """
    统一游标管理器

    负责管理采集游标的读取、比较和更新，确保所有采集器使用一致的逻辑。
    """

    def __init__(self, collector: 'BaseSlowQueryCollector'):
        self.collector = collector
        self.instance_name = collector.instance_name
        self._cursor: datetime = None
        self._max_cursor: datetime = None
        self._has_new_data: bool = False

    @property
    def cursor(self) -> datetime:
        """获取当前游标位置"""
        if self._cursor is None:
            self._cursor = self.collector.get_cursor()
        return self._cursor

    @property
    def max_cursor(self) -> datetime:
        """获取本次采集的最大游标位置"""
        return self._max_cursor or self.cursor

    @property
    def has_new_data(self) -> bool:
        """是否有新数据"""
        return self._has_new_data

    def is_new_data(self, data_time: datetime) -> bool:
        """
        判断数据是否是新数据（统一使用 > 比较）

        Args:
            data_time: 数据的时间戳

        Returns:
            True 如果数据是新的（大于游标时间）
        """
        if data_time is None:
            return False
        return data_time > self.cursor

    def update_max_cursor(self, data_time: datetime):
        """
        更新最大游标位置

        Args:
            data_time: 数据的时间戳
        """
        if data_time is None:
            return

        self._has_new_data = True
        if self._max_cursor is None or data_time > self._max_cursor:
            self._max_cursor = data_time

    def commit(self):
        """
        提交游标更新（只有在有新数据时才更新）

        Returns:
            bool: 是否更新了游标
        """
        if self._has_new_data and self._max_cursor:
            self.collector.update_cursor(self._max_cursor)
            logger.debug(f"[{self.instance_name}] 更新游标: {self._max_cursor}")
            return True
        return False


class BaseSlowQueryCollector(ABC):
    """
    慢查询采集器基类

    所有数据库类型的采集器都需要继承此类并实现：
    - collect_summary(): 采集统计数据
    - collect_detail(): 采集明细数据
    """

    def __init__(self, instance):
        self.instance = instance
        self.instance_id = instance.id
        self.instance_name = instance.instance_name
        self.db_type = instance.db_type

    @abstractmethod
    def collect_summary(self, start_time: datetime, end_time: datetime):
        """
        采集统计数据（按指纹聚合）

        Args:
            start_time: 开始时间
            end_time: 结束时间
        """
        pass

    @abstractmethod
    def collect_detail(self, start_time: datetime, end_time: datetime):
        """
        采集明细数据（具体SQL记录）

        Args:
            start_time: 开始时间
            end_time: 结束时间
        """
        pass

    def create_cursor_manager(self) -> CursorManager:
        """
        创建游标管理器

        Returns:
            CursorManager 实例
        """
        return CursorManager(self)

    def generate_hash(self, text) -> str:
        """
        生成指纹的 MD5 hash

        Args:
            text: SQL/命令文本 (str 或 bytes)

        Returns:
            MD5 hash 字符串
        """
        if not text:
            return ""
        if isinstance(text, bytes):
            return hashlib.md5(text).hexdigest()
        return hashlib.md5(text.encode("utf-8")).hexdigest()

    def get_cursor(self) -> datetime:
        """
        获取上次采集游标

        Returns:
            上次采集的时间戳，如果不存在则返回默认值（7天前）
        """
        from sql.models import SlowQueryCursor

        try:
            cursor_obj = SlowQueryCursor.objects.get(
                instance_id=self.instance_id, db_type=self.db_type
            )
            return cursor_obj.last_cursor or (datetime.now() - timedelta(days=7))
        except SlowQueryCursor.DoesNotExist:
            # 默认从7天前开始采集
            return datetime.now() - timedelta(days=7)

    def update_cursor(self, new_cursor: datetime):
        """
        更新采集游标

        Args:
            new_cursor: 新的时间戳游标
        """
        from sql.models import SlowQueryCursor

        update_or_create_with_retry(
            SlowQueryCursor,
            lookup={"instance_id": self.instance_id, "db_type": self.db_type},
            defaults={"last_cursor": new_cursor},
        )
        logger.debug(f"[{self.instance_name}] 更新游标: {new_cursor}")

    def get_engine(self):
        """获取数据库引擎"""
        from sql.engines import get_engine

        return get_engine(instance=self.instance)

    def log_collect_result(self, collect_type: str, count: int):
        """记录采集结果日志"""
        if count > 0:
            logger.info(
                f"[{self.instance_name}] 采集{collect_type}完成，共 {count} 条记录"
            )
        else:
            logger.debug(f"[{self.instance_name}] 无新的{collect_type}数据")
