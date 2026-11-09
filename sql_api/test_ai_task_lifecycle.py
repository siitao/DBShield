# -*- coding: UTF-8 -*-
"""P1-12：AI 任务 stale 判定（pending 与 running 使用不同计时基准）。"""

import datetime as dt

import pytest

from sql.models import AIDiagnosisTask, AIOptimizeTask
from sql.services.diagnosis import _mark_stale_task_failed
from sql_api.api_slowquery import _mark_stale_optimize_task_failed


@pytest.mark.django_db
def test_optimize_pending_allows_queue_backlog(normal_user, db_instance):
    task = AIOptimizeTask.objects.create(
        user=normal_user,
        instance=db_instance,
        db_name="db1",
        db_type="mysql",
        sql_hash="hash1",
        status="pending",
    )
    # 20 分钟前创建，仍低于 pending 阈值（30 分钟）
    AIOptimizeTask.objects.filter(id=task.id).update(
        created_at=dt.datetime.now() - dt.timedelta(minutes=20)
    )
    task.refresh_from_db()
    assert _mark_stale_optimize_task_failed(task) is False
    assert task.status == "pending"

    AIOptimizeTask.objects.filter(id=task.id).update(
        created_at=dt.datetime.now() - dt.timedelta(minutes=31)
    )
    task.refresh_from_db()
    assert _mark_stale_optimize_task_failed(task) is True
    task.refresh_from_db()
    assert task.status == "failed"


@pytest.mark.django_db
def test_optimize_running_uses_started_at(normal_user, db_instance):
    task = AIOptimizeTask.objects.create(
        user=normal_user,
        instance=db_instance,
        db_name="db1",
        db_type="mysql",
        sql_hash="hash2",
        status="running",
    )
    # created_at 很早，但刚开始执行：不应判 stale
    AIOptimizeTask.objects.filter(id=task.id).update(
        created_at=dt.datetime.now() - dt.timedelta(hours=2),
        started_at=dt.datetime.now(),
    )
    task.refresh_from_db()
    assert _mark_stale_optimize_task_failed(task) is False

    AIOptimizeTask.objects.filter(id=task.id).update(
        started_at=dt.datetime.now() - dt.timedelta(minutes=16)
    )
    task.refresh_from_db()
    assert _mark_stale_optimize_task_failed(task) is True


@pytest.mark.django_db
def test_diagnosis_pending_allows_queue_backlog(normal_user, db_instance):
    task = AIDiagnosisTask.objects.create(
        user=normal_user,
        instance=db_instance,
        db_name="db1",
        sql_hash="hash3",
        status="pending",
    )
    AIDiagnosisTask.objects.filter(id=task.id).update(
        created_at=dt.datetime.now() - dt.timedelta(minutes=20)
    )
    task.refresh_from_db()
    assert _mark_stale_task_failed(task) is False

    AIDiagnosisTask.objects.filter(id=task.id).update(
        created_at=dt.datetime.now() - dt.timedelta(minutes=31)
    )
    task.refresh_from_db()
    assert _mark_stale_task_failed(task) is True


@pytest.mark.django_db
def test_diagnosis_running_uses_started_at(normal_user, db_instance):
    task = AIDiagnosisTask.objects.create(
        user=normal_user,
        instance=db_instance,
        db_name="db1",
        sql_hash="hash4",
        status="running",
    )
    AIDiagnosisTask.objects.filter(id=task.id).update(
        created_at=dt.datetime.now() - dt.timedelta(hours=2),
        started_at=dt.datetime.now(),
    )
    task.refresh_from_db()
    assert _mark_stale_task_failed(task) is False

    AIDiagnosisTask.objects.filter(id=task.id).update(
        started_at=dt.datetime.now() - dt.timedelta(minutes=16)
    )
    task.refresh_from_db()
    assert _mark_stale_task_failed(task) is True
