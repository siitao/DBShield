# -*- coding: UTF-8 -*-
"""AI 慢查诊断：新增诊断任务/报告/反馈模型 + use_ai_diagnosis 权限"""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("sql", "0001_initial"),
    ]

    operations = [
        # 新增权限
        migrations.AlterModelOptions(
            name="permission",
            options={"permissions": (
                ("menu_dashboard", "菜单 Dashboard"),
                ("menu_sqlcheck", "菜单 SQL审核"),
                ("menu_sqlworkflow", "菜单 SQL上线"),
                ("menu_sqlanalyze", "菜单 SQL分析"),
                ("menu_query", "菜单 SQL查询"),
                ("menu_sqlquery", "菜单 在线查询"),
                ("menu_queryapplylist", "菜单 权限管理"),
                ("menu_sqloptimize", "菜单 SQL优化"),
                ("menu_sqladvisor", "菜单 优化工具"),
                ("menu_slowquery", "菜单 慢查日志"),
                ("menu_instance", "菜单 实例管理"),
                ("menu_instance_list", "菜单 实例列表"),
                ("menu_dbdiagnostic", "菜单 会话管理"),
                ("menu_database", "菜单 数据库管理"),
                ("menu_instance_account", "菜单 实例账号管理"),
                ("menu_param", "菜单 参数配置"),
                ("menu_param_compare", "菜单 参数对比"),
                ("menu_data_dictionary", "菜单 数据字典"),
                ("menu_tools", "菜单 工具插件"),
                ("menu_archive", "菜单 数据归档"),
                ("menu_my2sql", "菜单 My2SQL"),
                ("menu_schemasync", "菜单 SchemaSync"),
                ("menu_system", "菜单 系统管理"),
                ("menu_document", "菜单 相关文档"),
                ("menu_openapi", "菜单 OpenAPI"),
                ("sql_submit", "提交SQL上线工单"),
                ("sql_review", "审核SQL上线工单"),
                ("sql_execute_for_resource_group", "执行SQL上线工单(资源组粒度)"),
                ("sql_execute", "执行SQL上线工单(仅自己提交的)"),
                ("sql_analyze", "执行SQL分析"),
                ("optimize_sqladvisor", "执行SQLAdvisor"),
                ("optimize_sqltuning", "执行SQLTuning"),
                ("optimize_soar", "执行SOAR"),
                ("query_applypriv", "申请查询权限"),
                ("query_mgtpriv", "管理查询权限"),
                ("query_review", "审核查询权限"),
                ("query_submit", "提交SQL查询"),
                ("query_all_instances", "可查询所有实例"),
                ("query_resource_group_instance", "可查询所在资源组内的所有实例"),
                ("process_view", "查看会话"),
                ("process_kill", "终止会话"),
                ("tablespace_view", "查看表空间"),
                ("trx_view", "查看事务信息"),
                ("trxandlocks_view", "查看锁信息"),
                ("instance_account_manage", "管理实例账号"),
                ("param_view", "查看实例参数列表"),
                ("param_edit", "修改实例参数"),
                ("data_dictionary_export", "导出数据字典"),
                ("archive_apply", "提交归档申请"),
                ("archive_review", "审核归档申请"),
                ("archive_mgt", "管理归档申请"),
                ("audit_user", "审计权限"),
                ("query_download", "在线查询下载权限"),
                ("offline_download", "离线下载权限"),
                ("menu_sqlexportworkflow", "菜单 数据导出"),
                ("sqlexport_submit", "提交数据导出"),
                ("use_ai_diagnosis", "使用AI慢查诊断"),
            )},
        ),
        # AIDiagnosisTask
        migrations.CreateModel(
            name="AIDiagnosisTask",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("db_name", models.CharField(blank=True, default="", max_length=128, verbose_name="数据库名")),
                ("sql_hash", models.CharField(db_index=True, max_length=128, verbose_name="SQL指纹哈希")),
                ("status", models.CharField(default="pending", max_length=16, verbose_name="任务状态")),
                ("model", models.CharField(blank=True, default="", max_length=64, verbose_name="AI模型")),
                ("prompt_tokens", models.IntegerField(default=0, verbose_name="prompt token数")),
                ("completion_tokens", models.IntegerField(default=0, verbose_name="completion token数")),
                ("error", models.TextField(blank=True, default="", verbose_name="错误信息")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("finished_at", models.DateTimeField(blank=True, null=True, verbose_name="完成时间")),
                ("instance", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="sql.instance", verbose_name="实例")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL, verbose_name="发起用户")),
            ],
            options={
                "verbose_name": "AI慢查诊断任务",
                "verbose_name_plural": "AI慢查诊断任务",
                "db_table": "ai_diagnosis_task",
                "indexes": [models.Index(fields=["instance", "db_name", "sql_hash"], name="idx_aidiag_inst_db_hash")],
            },
        ),
        # AIDiagnosisReport
        migrations.CreateModel(
            name="AIDiagnosisReport",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sql_hash", models.CharField(db_index=True, max_length=128, verbose_name="SQL指纹哈希")),
                ("root_cause", models.CharField(blank=True, default="", max_length=200, verbose_name="根因")),
                ("severity", models.CharField(default="unknown", max_length=16, verbose_name="严重度")),
                ("bottleneck_type", models.CharField(blank=True, default="other", max_length=32, verbose_name="瓶颈类型")),
                ("evidence", models.JSONField(blank=True, default=list, verbose_name="证据列表")),
                ("suggestions", models.JSONField(blank=True, default=list, verbose_name="优化建议")),
                ("report_markdown", models.TextField(blank=True, default="", verbose_name="Markdown报告")),
                ("confidence", models.FloatField(default=0.0, verbose_name="置信度")),
                ("model", models.CharField(blank=True, default="", max_length=64, verbose_name="AI模型")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("task", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="report", to="sql.aidiagnosistask", verbose_name="诊断任务")),
            ],
            options={
                "verbose_name": "AI慢查诊断报告",
                "verbose_name_plural": "AI慢查诊断报告",
                "db_table": "ai_diagnosis_report",
            },
        ),
        # AIDiagnosisFeedback
        migrations.CreateModel(
            name="AIDiagnosisFeedback",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("helpful", models.BooleanField(default=True, verbose_name="是否有帮助")),
                ("reason", models.CharField(blank=True, default="", max_length=255, verbose_name="反馈原因")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("report", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="sql.aidiagnosisreport", verbose_name="诊断报告")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL, verbose_name="用户")),
            ],
            options={
                "verbose_name": "AI慢查诊断反馈",
                "verbose_name_plural": "AI慢查诊断反馈",
                "db_table": "ai_diagnosis_feedback",
            },
        ),
    ]
