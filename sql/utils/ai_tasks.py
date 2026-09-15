# -*- coding: UTF-8 -*-
"""AI 用量流水清理任务。

AIUsageLog 是只增不改的记账流水，长期运行会持续膨胀；按保留天数
（默认 180 天，可经 SysConfig `ai_usage_retention_days` 覆盖）分批清理。
"""

import datetime as _dt
import logging

logger = logging.getLogger("default")

# 默认保留天数
DEFAULT_AI_USAGE_RETENTION_DAYS = 180
# 每批删除行数，避免长事务
_BATCH_SIZE = 5000


def cleanup_ai_usage_log_task(days=None):
    """清理过期的 AI 用量流水。

    :param days: 保留天数；None 时读 SysConfig `ai_usage_retention_days`，
        未配置则用默认 180 天
    :return: 删除的总行数
    """
    from common.config import SysConfig
    from sql.models import AIUsageLog

    if days is None:
        try:
            days = int(
                SysConfig().get("ai_usage_retention_days")
                or DEFAULT_AI_USAGE_RETENTION_DAYS
            )
        except (TypeError, ValueError):
            days = DEFAULT_AI_USAGE_RETENTION_DAYS

    cutoff = _dt.datetime.now() - _dt.timedelta(days=days)
    total_deleted = 0
    while True:
        ids = list(
            AIUsageLog.objects.filter(created_at__lt=cutoff)
            .order_by("id")
            .values_list("id", flat=True)[:_BATCH_SIZE]
        )
        if not ids:
            break
        deleted, _ = AIUsageLog.objects.filter(id__in=ids).delete()
        total_deleted += deleted
        if deleted < _BATCH_SIZE:
            break
    if total_deleted:
        logger.info(f"AI 用量流水清理完成（保留 {days} 天）: 删除 {total_deleted} 条")
    return total_deleted
