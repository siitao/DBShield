"""
SQL 分析 / SQL 优化 DRF APIView 集

路由：
  POST /api/v1/sql_analyze/generate/      — 解析上传文件为 SQL 列表
  POST /api/v1/sql_analyze/analyze/       — SOAR 分析 SQL
  POST /api/v1/sql_analyze/ai/            — AI 分析 SQL
  POST /api/v1/optimize/sqladvisor/       — SQLAdvisor 建议
  POST /api/v1/optimize/soar/             — SOAR 建议（markdown）
  POST /api/v1/optimize/sqltuning/        — MySQL 调优
  POST /api/v1/optimize/explain/          — 执行计划
  POST /api/v1/optimize/ai/               — AI 优化建议

注意：慢查询相关 API 已移至 api_slowquery_v2.py
"""
import logging
import re
from pathlib import Path

import sqlparse
from common.config import SysConfig
from common.utils.extend_json_encoder import encode_json as _encode
from common.utils.openai import OpenaiClient, check_openai_config
from django.http import JsonResponse
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.views import APIView
from sql.engines import get_engine
from sql.models import Instance
from sql.plugins.soar import Soar
from sql.plugins.sqladvisor import SQLAdvisor
from sql.services.instance_service import resolve_instance
from sql.sql_tuning import SqlTuning
from sql.utils.resource_group import user_instances
from sql.utils.sql_utils import extract_tables, generate_sql
from sql_api.api_slowquery_v2 import mask_sql_literals, sanitize_explain_sql

logger = logging.getLogger("default")


# ---------- permissions ----------


class SqlAnalyzePermission(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return u and u.is_authenticated and (u.is_superuser or u.has_perm("sql.sql_analyze"))


class SqlOptimizePermission(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return u and u.is_authenticated and (u.is_superuser or u.has_perm("sql.menu_sqladvisor"))


# ---------- helpers ----------


def _get_and_check_instance(user, instance_name, db_type=None):
    """获取实例并做权限校验。"""
    if not instance_name:
        raise Instance.DoesNotExist
    i = Instance.objects.get(instance_name=instance_name)
    if db_type:
        user_instances(user, db_type=[db_type]).get(instance_name=instance_name)
    else:
        user_instances(user, db_type=[i.db_type]).get(instance_name=instance_name)
    return i


# ========== SQL 分析 ==========


class SqlAnalyzeGenerateView(APIView):
    """解析 SQL 文本为列表"""

    permission_classes = [IsAuthenticated, SqlAnalyzePermission]

    def post(self, request):
        text = request.data.get("text")
        if text is None:
            result = {"total": 0, "rows": []}
        else:
            rows = generate_sql(text)
            result = {"total": len(rows), "rows": rows}
        return JsonResponse(_encode(result), safe=False)


class SqlAnalyzeAnalyzeView(APIView):
    """SOAR 分析 SQL"""

    permission_classes = [IsAuthenticated, SqlAnalyzePermission]

    def post(self, request):
        text = request.data.get("text")
        instance_name = request.data.get("instance_name")
        db_name = request.data.get("db_name")
        if not text:
            result = {"total": 0, "rows": []}
            return JsonResponse(_encode(result), safe=False)

        soar = Soar()
        online_dsn = ""
        soar_test_dsn = ""
        if instance_name and db_name:
            try:
                instance = _get_and_check_instance(request.user, instance_name, db_type="mysql")
            except Instance.DoesNotExist:
                return JsonResponse({"status": 1, "msg": "你所在组未关联该实例！", "data": []})
            soar_test_dsn = SysConfig().get("soar_test_dsn") or ""
            user, password = instance.get_username_password()
            online_dsn = f"{user}:{password}@{instance.host}:{instance.port}/{db_name}"

        args = {
            "report-type": "markdown",
            "query": "",
            "online-dsn": online_dsn,
            "test-dsn": soar_test_dsn,
            "allow-online-as-test": False,
        }
        rows = generate_sql(text)
        for row in rows:
            try:
                p = Path(row["sql"].strip())
                if p.exists():
                    return JsonResponse({"status": 1, "msg": "SQL 语句不合法", "data": []})
            except OSError:
                pass
            args["query"] = row["sql"]
            cmd_args = soar.generate_args2cmd(args=args)
            stdout, stderr = soar.execute_cmd(cmd_args).communicate()
            row["report"] = stdout if stdout else stderr

        result = {"total": len(rows), "rows": rows}
        return JsonResponse(_encode(result), safe=False)


class SqlAnalyzeAIView(APIView):
    """AI 分析 SQL"""

    permission_classes = [IsAuthenticated, SqlAnalyzePermission]

    def post(self, request):
        text = request.data.get("text")
        if not text:
            return JsonResponse({"status": 1, "msg": "SQL 语句不能为空"})

        if not check_openai_config():
            return JsonResponse({"status": 1, "msg": "未配置 OpenAI API"})

        try:
            client = OpenaiClient()
            # H4 回灌：SQL 原样外发前先做字面量脱敏（与 v2 诊断链路口径一致）
            prompt = (
                f"请分析以下 SQL 语句的性能问题并给出优化建议：\n\n"
                f"```sql\n{mask_sql_literals(text)}\n```"
            )
            result = client.chat(prompt)
            return JsonResponse({"status": 0, "msg": "success", "data": result})
        except Exception as e:
            logger.error(f"AI 分析失败: {e}", exc_info=True)
            return JsonResponse({"status": 1, "msg": "AI 分析失败，请查看服务端日志"})


# ========== SQL 优化 ==========


class OptimizeSqlAdvisorView(APIView):
    """SQLAdvisor 索引优化建议"""

    permission_classes = [IsAuthenticated, SqlOptimizePermission]

    def post(self, request):
        sql_content = request.data.get("sql_content")
        instance_name = request.data.get("instance_name")
        db_name = request.data.get("db_name")
        verbose = request.data.get("verbose", 1)

        # 参数验证
        if not sql_content or not instance_name:
            return JsonResponse({"status": 1, "msg": "页面提交参数可能为空"})

        # 实例权限校验（限 mysql）
        try:
            instance = _get_and_check_instance(request.user, instance_name, db_type="mysql")
        except Instance.DoesNotExist:
            return JsonResponse({"status": 1, "msg": "你所在组未关联该实例！"})

        # 检查 sqladvisor 程序路径
        sqladvisor_path = SysConfig().get("sqladvisor") or "sqladvisor"
        sqladvisor = SQLAdvisor(sqladvisor_path)

        # 获取连接信息
        user, password = instance.get_username_password()
        online_dsn = f"{user}:{password}@{instance.host}:{instance.port}/{db_name}"

        # 执行 SQLAdvisor
        args = {
            "query": sql_content,
            "online-dsn": online_dsn,
            "verbose": verbose,
        }
        cmd_args = sqladvisor.generate_args2cmd(args=args)
        stdout, stderr = sqladvisor.execute_cmd(cmd_args).communicate()

        result = stdout if stdout else stderr
        return JsonResponse({"status": 0, "msg": "success", "data": result})


class OptimizeSoarView(APIView):
    """SOAR 建议（markdown）"""

    permission_classes = [IsAuthenticated, SqlOptimizePermission]

    def post(self, request):
        sql_content = request.data.get("sql")
        instance_name = request.data.get("instance_name")
        db_name = request.data.get("db_name")

        if not sql_content:
            return JsonResponse({"status": 1, "msg": "SQL 语句不能为空"})

        soar = Soar()
        online_dsn = ""
        soar_test_dsn = ""

        if instance_name and db_name:
            try:
                instance = _get_and_check_instance(request.user, instance_name, db_type="mysql")
            except Instance.DoesNotExist:
                return JsonResponse({"status": 1, "msg": "你所在组未关联该实例！"})
            soar_test_dsn = SysConfig().get("soar_test_dsn") or ""
            user, password = instance.get_username_password()
            online_dsn = f"{user}:{password}@{instance.host}:{instance.port}/{db_name}"

        args = {
            "report-type": "markdown",
            "query": sql_content,
            "online-dsn": online_dsn,
            "test-dsn": soar_test_dsn,
            "allow-online-as-test": False,
        }
        cmd_args = soar.generate_args2cmd(args=args)
        stdout, stderr = soar.execute_cmd(cmd_args).communicate()

        result = stdout if stdout else stderr
        return JsonResponse({"status": 0, "msg": "success", "data": result})


class OptimizeSqlTuningView(APIView):
    """MySQL 调优"""

    permission_classes = [IsAuthenticated, SqlOptimizePermission]

    def post(self, request):
        sql_content = request.data.get("sql_content")
        instance_name = request.data.get("instance_name")
        db_name = request.data.get("db_name")
        option = request.data.get("option", [])

        # 参数验证
        if not sql_content or not instance_name or not db_name:
            return JsonResponse({"status": 1, "msg": "页面提交参数可能为空"})

        # 实例权限校验（限 mysql）
        try:
            instance = _get_and_check_instance(request.user, instance_name, db_type="mysql")
        except Instance.DoesNotExist:
            return JsonResponse({"status": 1, "msg": "你所在组未关联该实例！"})

        # 执行调优
        try:
            tuning = SqlTuning(instance, db_name)
            result = tuning.tuning(sql_content, option)
            return JsonResponse({"status": 0, "msg": "success", "data": result})
        except Exception as e:
            logger.error(f"SQL 调优失败: {e}", exc_info=True)
            return JsonResponse({"status": 1, "msg": "SQL 调优失败，请查看服务端日志"})


class ExplainSqlView(APIView):
    """执行计划"""

    permission_classes = [IsAuthenticated, SqlOptimizePermission]

    def post(self, request):
        sql_content = request.data.get("sql_content")
        instance_name = request.data.get("instance_name")
        db_name = request.data.get("db_name")

        # 参数验证
        if not sql_content or not instance_name or not db_name:
            return JsonResponse({"status": 1, "msg": "页面提交参数可能为空"})

        # 实例权限校验
        try:
            instance = _get_and_check_instance(request.user, instance_name, db_type="mysql")
        except Instance.DoesNotExist:
            return JsonResponse({"status": 1, "msg": "你所在组未关联该实例！"})

        # 执行 EXPLAIN（H4 回灌：与 v2 共用 sanitize_explain_sql 闸门——
        # 截首句、剥注释、SELECT/WITH 白名单、拒 INTO OUTFILE/DUMPFILE，
        # 防 EXPLAIN ANALYZE 真实执行写文件等注入面）
        clean_sql, reject_reason = sanitize_explain_sql(sql_content)
        if clean_sql is None:
            return JsonResponse({"status": 1, "msg": reject_reason})
        # 参数化模板 SQL 的 '?' 占位符替换为字面量 1（EXPLAIN 只做计划不执行）
        clean_sql = clean_sql.replace("?", "1")
        try:
            engine = get_engine(instance=instance)
            result = engine.query(
                db_name=db_name,
                sql=f"EXPLAIN {clean_sql}",
                max_execution_time=30000,
            )
            column_list = result.column_list
            rows = result.rows
            return JsonResponse({
                "status": 0,
                "msg": "success",
                "data": {"column_list": column_list, "rows": rows}
            })
        except Exception as e:
            logger.error(f"获取执行计划失败: {e}", exc_info=True)
            return JsonResponse({"status": 1, "msg": "获取执行计划失败，请查看服务端日志"})


class OptimizeAIView(APIView):
    """AI 优化建议"""

    permission_classes = [IsAuthenticated, SqlOptimizePermission]

    def post(self, request):
        sql_content = request.data.get("sql_content")
        instance_name = request.data.get("instance_name")
        db_name = request.data.get("db_name")

        if not sql_content:
            return JsonResponse({"status": 1, "msg": "SQL 语句不能为空"})

        if not check_openai_config():
            return JsonResponse({"status": 1, "msg": "未配置 OpenAI API"})

        # 获取表结构信息
        table_info = ""
        if instance_name and db_name:
            try:
                instance = _get_and_check_instance(request.user, instance_name, db_type="mysql")
                engine = get_engine(instance=instance)
                tables = extract_tables(sql_content)
                for table in tables:
                    # 表名仅作标识符使用：白名单校验 + 反引号包裹，拒绝拼接注入
                    if not re.fullmatch(r"[\w$.]+", table):
                        continue
                    result = engine.query(
                        db_name=db_name, sql=f"SHOW CREATE TABLE `{table}`"
                    )
                    if result.rows:
                        table_info += f"\n{result.rows[0][1]}\n"
            except Exception as e:
                logger.warning(f"获取表结构失败: {e}")

        try:
            client = OpenaiClient()
            # H4 回灌：SQL 与 DDL（COMMENT/DEFAULT 常含真实业务数据）外发前统一字面量脱敏
            prompt = f"""请分析以下 SQL 语句并给出优化建议：

```sql
{mask_sql_literals(sql_content)}
```

{f'表结构信息：{mask_sql_literals(table_info)}' if table_info else ''}

请从以下几个方面分析：
1. 索引优化
2. SQL 语法优化
3. 查询重写建议
4. 性能瓶颈分析"""

            result = client.chat(prompt)
            return JsonResponse({"status": 0, "msg": "success", "data": result})
        except Exception as e:
            logger.error(f"AI 优化建议失败: {e}", exc_info=True)
            return JsonResponse({"status": 1, "msg": "AI 优化建议失败，请查看服务端日志"})
