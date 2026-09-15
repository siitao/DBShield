# -*- coding:utf-8 -*-
"""慢查询数据保留策略清理：明细按入库时间、统计按最后出现时间的过期清理测试"""
import datetime

import pytest

from sql.collectors.tasks import cleanup_slowquery_data_task, get_retention_days
from sql.models import MySQLSlowQueryDetail, MySQLSlowQuerySummary

NOW = datetime.datetime(2026, 9, 15, 12, 0, 0)


def _summary(instance, sql_hash, last_seen):
    return MySQLSlowQuerySummary.objects.create(
        instance=instance,
        sql_hash=sql_hash,
        fingerprint=f"SELECT * FROM t WHERE id = '{sql_hash}'",
        db_name="some_db",
        last_seen=last_seen,
    )


@pytest.fixture
def fixed_now(mocker):
    """固定 timezone.now，让过期边界可精确断言"""
    from django.utils import timezone

    mocker.patch.object(timezone, "now", return_value=NOW)
    return NOW


@pytest.mark.django_db
def test_cleanup_deletes_stale_summary(db_instance, fixed_now):
    """last_seen 超过保留期的统计行被删除"""
    stale = _summary(db_instance, "stale_hash", NOW - datetime.timedelta(days=30))
    _summary(db_instance, "fresh_hash", NOW - datetime.timedelta(days=1))

    results = cleanup_slowquery_data_task(days=15)

    assert results["mysql_summary"] == 1
    assert not MySQLSlowQuerySummary.objects.filter(pk=stale.pk).exists()
    assert MySQLSlowQuerySummary.objects.filter(sql_hash="fresh_hash").exists()


@pytest.mark.django_db
def test_cleanup_keeps_active_summary_despite_old_created_at(db_instance, fixed_now):
    """持续活跃的统计行不因 created_at 很旧而被误删（统计按 last_seen 判断过期）"""
    active = _summary(db_instance, "long_lived_hash", NOW - datetime.timedelta(days=1))
    # 聚合任务只推进 last_seen/updated_at，行的入库时间可能远早于保留期
    MySQLSlowQuerySummary.objects.filter(pk=active.pk).update(
        created_at=NOW - datetime.timedelta(days=200)
    )

    cleanup_slowquery_data_task(days=15)

    assert MySQLSlowQuerySummary.objects.filter(pk=active.pk).exists()


@pytest.mark.django_db
def test_cleanup_deletes_expired_detail_by_created_at(db_instance, fixed_now):
    """明细行仍按入库时间清理"""
    detail = MySQLSlowQueryDetail.objects.create(
        instance=db_instance,
        sql_hash="hash_x",
        execution_start_time=NOW - datetime.timedelta(days=1),
        sql_text="SELECT SLEEP(2)",
        query_time=2.0,
    )
    MySQLSlowQueryDetail.objects.filter(pk=detail.pk).update(
        created_at=NOW - datetime.timedelta(days=30)
    )

    results = cleanup_slowquery_data_task(days=15)

    assert results["mysql_detail"] == 1
    assert not MySQLSlowQueryDetail.objects.filter(pk=detail.pk).exists()


@pytest.mark.django_db
def test_cleanup_result_keys_cover_detail_and_summary(db_instance, fixed_now):
    """返回结果同时包含明细与统计的清理计数"""
    results = cleanup_slowquery_data_task(days=15)

    for key in (
        "mysql_detail", "pgsql_detail", "mongo_detail", "redis_detail",
        "mysql_summary", "pgsql_summary", "mongo_summary", "redis_summary",
    ):
        assert key in results


@pytest.mark.django_db
def test_retention_days_config_applies(mocker, db_instance, fixed_now):
    """保留天数取自 slow_query_retention_days 配置"""
    mock_config = mocker.patch("common.config.SysConfig.get", return_value="15")

    assert get_retention_days() == 15
    mock_config.assert_called_with("slow_query_retention_days", 30)
