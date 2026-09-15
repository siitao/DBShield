# -*- coding:utf-8 -*-
"""慢查询定时任务注册：幂等注册、字段漂移修复与 next_run 保留测试

注意：0008 数据迁移在测试库建立时就会注册这批调度，用例必须容忍
前置数据存在（fixture 前置清理），不能假设空表。
"""
import ast
import datetime

import pytest
from django_q.models import Schedule

from sql.collectors.tasks import add_slowquery_collect_schedule

SCHEDULE_NAMES = [
    "慢查询采集-每5分钟",
    "慢查询聚合-每5分钟",
    "慢查询诊断任务清理-每10分钟",
    "MySQL慢日志清理-每天",
    "慢查询数据清理-每天",
]


def parse_literal(value):
    """Schedule.args/kwargs 是 TextField，还原存储的文本字面量"""
    if isinstance(value, str):
        return ast.literal_eval(value)
    return value


@pytest.fixture(autouse=True)
def clean_schedules(db):
    Schedule.objects.filter(name__in=SCHEDULE_NAMES).delete()
    yield
    Schedule.objects.filter(name__in=SCHEDULE_NAMES).delete()


@pytest.mark.django_db
def test_register_creates_all_schedules():
    add_slowquery_collect_schedule()

    names = list(
        Schedule.objects.filter(name__in=SCHEDULE_NAMES).values_list("name", flat=True)
    )
    assert sorted(names) == sorted(SCHEDULE_NAMES)


@pytest.mark.django_db
def test_register_is_idempotent():
    add_slowquery_collect_schedule()
    add_slowquery_collect_schedule()

    assert Schedule.objects.filter(name__in=SCHEDULE_NAMES).count() == len(SCHEDULE_NAMES)


@pytest.mark.django_db
def test_register_keeps_next_run_when_no_drift():
    """重复注册不重写定义、不回拨 next_run（模拟已运行多天的调度）"""
    add_slowquery_collect_schedule()
    s = Schedule.objects.get(name="慢查询数据清理-每天")
    running_next_run = (datetime.datetime.now() - datetime.timedelta(days=3)).replace(
        microsecond=0
    )
    Schedule.objects.filter(pk=s.pk).update(next_run=running_next_run)

    add_slowquery_collect_schedule()

    after = Schedule.objects.get(name="慢查询数据清理-每天")
    assert after.pk == s.pk
    assert after.next_run == running_next_run


@pytest.mark.django_db
def test_register_repairs_drift_without_resetting_next_run():
    """已存在的同名调度：错误字段被修复，next_run 不被回拨"""
    next_run = (datetime.datetime.now() + datetime.timedelta(days=5)).replace(
        microsecond=0
    )
    Schedule.objects.create(
        name="慢查询数据清理-每天",
        func="some.wrong.func",
        schedule_type=Schedule.DAILY,
        repeats=-1,
        next_run=next_run,
    )

    add_slowquery_collect_schedule()

    s = Schedule.objects.get(name="慢查询数据清理-每天")
    assert s.func == "sql.collectors.tasks.cleanup_slowquery_data_task"
    assert parse_literal(s.kwargs) == {"timeout": 600}
    assert s.next_run == next_run


@pytest.mark.django_db
def test_daily_schedules_next_run_at_documented_hour():
    """每日调度首次注册时落在文档声明的凌晨2点/3点，且不早于当前时间"""
    add_slowquery_collect_schedule()

    mysql_log = Schedule.objects.get(name="MySQL慢日志清理-每天")
    assert mysql_log.next_run.hour == 2
    assert mysql_log.next_run > datetime.datetime.now()

    data_cleanup = Schedule.objects.get(name="慢查询数据清理-每天")
    assert data_cleanup.next_run.hour == 3
    assert data_cleanup.next_run > datetime.datetime.now()


@pytest.mark.django_db
def test_minute_schedules_carry_timeout():
    """采集/清理任务必须带 timeout，否则走集群默认 60s 必然超时"""
    add_slowquery_collect_schedule()

    for name in SCHEDULE_NAMES:
        s = Schedule.objects.get(name=name)
        assert parse_literal(s.kwargs).get("timeout"), f"{name} 缺少 timeout 配置"
