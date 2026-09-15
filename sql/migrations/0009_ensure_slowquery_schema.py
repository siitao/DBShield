# -*- coding:utf-8 -*-
"""慢查询本地表结构自动引导（建表 + 补索引）。

慢查相关模型均为 managed=False，migrate 本不为它们建表，首次部署依赖手工执行
src/init_sql/slow_query/*.sql 与 add_indexes.sql（后者不幂等，MySQL 无
CREATE INDEX IF NOT EXISTS）。改为 migrate 时幂等引导：缺表建表、缺索引补建，
首次部署无需任何手工步骤。
"""
from django.db import migrations


def ensure_schema(apps, schema_editor):
    from sql.services.slowquery_schema import ensure_slowquery_schema

    ensure_slowquery_schema(schema_editor.connection)


class Migration(migrations.Migration):

    dependencies = [
        ('sql', '0008_register_slowquery_schedules'),
    ]

    # 涉及 DDL：MySQL 不支持事务性 DDL，显式声明非原子
    atomic = False

    operations = [
        # 反向不删表：慢查表承载运行数据，回滚迁移不应清库
        migrations.RunPython(ensure_schema, migrations.RunPython.noop),
    ]
