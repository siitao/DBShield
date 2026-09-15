# -*- coding:utf-8 -*-
"""慢查询相关定时任务自动注册。

此前 add_slowquery_collect_schedule() 没有任何调用方，采集/清理调度依赖
人工在 Django Admin 手工创建，导致慢查询数据保留策略（slow_query_retention_days）
实际不生效。改为 migrate 时幂等注册，随部署自动补齐。
"""
from django.db import migrations


def register_schedules(apps, schema_editor):
    from sql.collectors.tasks import add_slowquery_collect_schedule

    add_slowquery_collect_schedule()


class Migration(migrations.Migration):

    dependencies = [
        ('sql', '0007_delete_redisslowquery_delete_redisslowqueryhistory_and_more'),
        # 注册内容写入 django_q_schedule，须在其建表迁移之后执行
        ('django_q', '0018_task_success_index'),
    ]

    operations = [
        # 反向不清除：已注册的调度是运行态数据，回滚迁移不应摘除调度
        migrations.RunPython(register_schedules, migrations.RunPython.noop),
    ]
